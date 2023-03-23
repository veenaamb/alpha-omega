from core.settings import STATIC_ROOT
import os
from datetime import datetime, timezone
import markdown
import zipfile
from packageurl import PackageURL
import logging
import io
import json
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    HttpResponseNotFound,
)
from oaffe.models import (
    Assertion,
    Policy,
    Subject,
    PolicyEvaluationResult,
    AssertionGenerator,
    PolicyGroup,
    PackageRequest,
)
from oaffe.utils.policy import refresh_policies
from django.shortcuts import get_object_or_404
from django.core.paginator import Paginator

def home(request: HttpRequest) -> HttpResponse:
    """Home page view for the OAF UI."""
    return render(request, "home.html", {'page_title': 'Assurance Assertions Home'})


def api_get_assertion(request: HttpRequest, assertion_uuid: str) -> JsonResponse:
    assertion = Assertion.objects.get(uuid=assertion_uuid)
    return JsonResponse(assertion.to_dict())

def clamp(value, min_value, max_value):
    """Clamp a value between a minimum and maximum value."""
    return max(min_value, min(float(value), max_value))

def search_subjects(request: HttpRequest) -> HttpResponse:
    """Searches the database for subjects that match a given query.
    Searches are basic, case-insensitive 'contains'.
    """
    query = request.GET.get("q")
    if query:

        page_size = clamp(request.GET.get("page_size", 20), 10, 500)
        page = clamp(request.GET.get("page", 1), 1, 1000)

        subjects = Subject.objects.filter(identifier__icontains=query)

        subject_map = {}
        for subject in subjects:
            cur_version = None

            if subject.subject_type == Subject.SUBJECT_TYPE_PACKAGE_URL:
                top_identifier = PackageURL.from_string(subject.identifier).to_dict()
                cur_version = top_identifier['version']
                top_identifier['version'] = None
                top_identifier = PackageURL(**top_identifier)
            elif subject.subject_type == Subject.SUBJECT_TYPE_GITHUB_URL:
                top_identifier = subject.identifier
            else:
                raise ValueError("Unexpected subject type.")

            top_identifier = str(top_identifier)

            if top_identifier not in subject_map:
                subject_map[top_identifier] = []

            if cur_version:
                subject_map[top_identifier].append({
                    'uuid': str(subject.uuid),
                    'version': cur_version
                })

        subject_map = [(k, v) for k, v in subject_map.items()]
        paginator = Paginator(subject_map, page_size)

        query_string = request.GET.copy()
        if "page" in query_string:
            query_string.pop("page", None)

        context = {
            "query": query,
            "subjects": paginator.get_page(page),
            "params": query_string.urlencode(),
        }
        return render(request, "search.html", context)
    else:
        return HttpResponseRedirect("/")


def refresh(request: HttpRequest) -> HttpResponse:
    refresh_policies()
    return HttpResponseRedirect("/")

def data_dump(request: HttpRequest) -> HttpResponse:
    dump_path = STATIC_ROOT
    if not dump_path:
        dump_path = os.path.abspath(os.path.join(__file__, '../../oaffe/static/oaffe'))
    filename = os.path.join(dump_path, 'policy_evaluations.csv')

    if os.path.isfile(filename):
        stat_result = os.stat(filename)
        _date = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)
        c = {
            'generated_date': _date
        }
    else:
        c = {}

    return render(request, 'data_dump.html', c)

def download_assertion(request: HttpRequest, assertion_uuid: str) -> HttpResponse:
    """
    Downloads a specific assertion (by UUID).
    """
    assertion = Assertion.objects.get(uuid=assertion_uuid)
    response = HttpResponse(json.dumps(assertion.content, indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="oaf-{assertion_uuid}.json"'
    return response


def download_assertions(request: HttpRequest) -> HttpResponse:
    """Downloads all assertions as a zip file for the given subject."""
    subject_uuid = request.GET.get("subject_uuid")
    if not subject_uuid:
        return HttpResponseBadRequest("Missing subject uuid.")

    assertions = Assertion.objects.filter(subject__uuid=subject_uuid)
    if assertions.exists():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED, False) as zf:
            for assertion in assertions:
                content = json.dumps(assertion.content, indent=2)
                zf.writestr(f"{assertion.uuid}.json", content)

        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="oaf-{subject_uuid}.zip"'
        return response
    else:
        return HttpResponseNotFound()


def policy_heatmap(request: HttpRequest) -> HttpResponse:
    policies = Policy.objects.all()
    subjects = set([p.subject for p in policies])
    result = {}
    for subject in subjects:
        if subject not in result:
            result[subject] = {}

        for policy in policies:
            if policy.subject == subject:
                result[subject][policy.policy] = policy.status

    c = {"result": result}

    return render(request, "heatmap.html", c)


def show_assertions(request: HttpRequest) -> HttpResponse:
    subject_uuid = request.GET.get("subject_uuid")
    if subject_uuid:
        subject = get_object_or_404(Subject, pk=subject_uuid)
        assertions = Assertion.objects.filter(subject=subject)
        policy_evaluation_results = PolicyEvaluationResult.objects.filter(subject=subject)

        policy_group_uuid = request.GET.get("policy_group_uuid")
        if policy_group_uuid:
            policy_group = get_object_or_404(PolicyGroup, pk=policy_group_uuid)
            policy_evaluation_results = policy_evaluation_results.filter(
                policy__in=policy_group.policies.all()
            )

        other_subjects = []

        if subject.subject_type == Subject.SUBJECT_TYPE_PACKAGE_URL:
            purl = PackageURL.from_string(subject.identifier).to_dict()
            purl["version"] = None
            purl = PackageURL(**purl)

            related_subjects = Subject.objects.filter(
                subject_type=Subject.SUBJECT_TYPE_PACKAGE_URL,
                identifier__startswith=str(purl),
            ).exclude(identifier=subject.identifier)

        c = {
            "subject": subject,
            "assertions": assertions,
            "policy_evaluation_results": policy_evaluation_results,
            "related_subjects": sorted(subject.get_versions(), key=lambda x: x.identifier),
            "policy_group_uuid": policy_group_uuid,
            "policy_groups": PolicyGroup.objects.all(),
        }
        return render(request, "view.html", c)

    else:
        return HttpResponseRedirect("/")


def package_request(request: HttpRequest) -> HttpResponse:
    """Handles interaction with the 'request a package' function."""
    if request.method == "POST":
        content = request.POST.get("package_list")
        if content:
            for _line in content.splitlines():
                line = _line.strip()
                if not line or len(line) > 500:
                    continue
                PackageRequest(package=line).save()
            return HttpResponseRedirect("/package_request?action=complete")
    else:
        return render(request, "package_request.html", {"complete": request.GET.get("action") == "complete"})


def api_get_help(request: HttpRequest) -> JsonResponse:
    """Retrieve help text for a given policy or assertion generator."""
    _type = request.GET.get("type")
    if _type == "policy":
        policy_uuid = request.GET.get("policy_uuid")
        policy = get_object_or_404(Policy, pk=policy_uuid)
        return HttpResponse(markdown.markdown(policy.help_text or ""))

    elif _type == "assertion_generator":
        assertion_generator_uuid = request.GET.get("assertion_generator_uuid")
        generator = get_object_or_404(AssertionGenerator, pk=assertion_generator_uuid)
        return HttpResponse(markdown.markdown(generator.help_text or ""))

    return HttpResponseBadRequest("Help not found.")


@csrf_exempt
def api_add_assertion(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.POST.get("assertion"))
    except:
        return HttpResponseBadRequest("Invalid assertion.")

    # Extract generator
    generator_name = data.get("predicate", {}).get("generator", {}).get("name") or ""
    generator_version = data.get("predicate", {}).get("generator", {}).get("version") or ""
    generator, _ = AssertionGenerator.objects.get_or_create(
        name=generator_name, version=generator_version, defaults={"name_readable": generator_name}
    )

    # Extract subject
    subject_type = data.get("subject", {}).get("type") or ""
    if subject_type == Subject.SUBJECT_TYPE_PACKAGE_URL:
        subject_identifier = data.get("subject", {}).get("purl")
    elif subject_type == Subject.SUBJECT_TYPE_GITHUB_URL:
        subject_identifier = data.get("subject", {}).get("github_url")
    else:
        return HttpResponseBadRequest("Invalid subject.")
    subject, _ = Subject.objects.get_or_create(subject_type=subject_type, identifier=subject_identifier)

    # Extract created date
    created_date = data.get("predicate", {}).get("operational", {}).get("timestamp")

    assertion = Assertion()
    assertion.generator = generator
    assertion.subject = subject
    assertion.content = data
    assertion.created_date = created_date
    assertion.save()

    refresh_policies(subject)

    return JsonResponse({"success": True})


def _get_subject_from_request(request: HttpRequest) -> Subject:
    """Retrieve a subject from a request."""
    if "subject_uuid" in request.GET:
        subject_uuid = request.GET.get("subject_uuid")
        logging.debug("Retrieving assertions for subject[%s]", subject_uuid)
        subject = get_object_or_404(Subject, uuid=subject_uuid)
    elif "subject_identifier" in request.GET:
        subject_identifier = request.GET.get("subject_identifier")
        logging.debug("Retrieving assertions for subject[%s]", subject_identifier)
        subject = get_object_or_404(Subject, identifier=subject_identifier)
    else:
        raise ValueError("Invalid subject.")

    if not subject:
        raise Http404("No subject found.")

    return subject

def api_get_assertions(request: HttpRequest) -> JsonResponse:
    """Retrieve assertions based on API parameters."""
    subject = _get_subject_from_request(request)
    assertions = Assertion.objects.filter(subject=subject)
    return JsonResponse(list(a.to_dict() for a in assertions), safe=False, json_dumps_params={'indent': 2})

def api_get_policy_evaluation_results(request: HttpRequest) -> JsonResponse:
    """Retrieve policy evaluations based on API parameters."""
    subject = _get_subject_from_request(request)

    results = PolicyEvaluationResult.objects.filter(subject=subject)

    # Further filter by policy group
    if 'policy_group_uuid' in request.GET:
        policy_group = get_object_or_404(PolicyGroup, uuid=request.GET.get('policy_group_uuid'))
        results = results.filter(policy__in=policy_group.policies.all())

    return JsonResponse(list(r.to_dict() for r in results), safe=False, json_dumps_params={'indent': 2})

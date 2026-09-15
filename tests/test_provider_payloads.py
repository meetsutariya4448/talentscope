import pytest

from app.ingestion.provider_payloads import extract_job_list


def test_extract_job_list_accepts_direct_and_nested_provider_payloads():
    jobs = [{"id": "one"}, {"id": "two"}]

    assert extract_job_list(jobs) == jobs
    assert extract_job_list({"jobs": jobs}, key="jobs") == jobs


@pytest.mark.parametrize(
    ("payload", "key"),
    [
        (None, None),
        ({"jobs": None}, "jobs"),
        ({"unexpected": []}, "jobs"),
        ([{"id": "one"}, "bad-entry"], None),
    ],
)
def test_extract_job_list_rejects_untrustworthy_provider_payloads(payload, key):
    with pytest.raises(ValueError, match="provider"):
        extract_job_list(payload, key=key)

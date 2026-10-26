"""Friendly status labels are display-only: filters and raw JSON keep the internal values."""

import re

from foundation.ui.labels import STATUS_LABELS, status_label
from tests.refunds.factories import make_refund
from tests.refunds.helpers import client_as

BIG = 150_000  # $1,500


def test_status_label_mapping():
    assert status_label("partially_approved") == "Awaiting second approval"
    assert status_label("payment_unknown") == "Payment outcome unknown"
    assert status_label("payment_failed") == "Payment failed"
    assert status_label("pending") == "Pending"
    assert status_label("succeeded") == "Succeeded"
    assert status_label("something_new") == "something_new"
    assert status_label(None) == ""
    assert all(label[0].isupper() for label in STATUS_LABELS.values())


def test_dashboard_shows_labels_and_filters_by_internal_value(database):
    waiting = make_refund(BIG, status="partially_approved", first_approved_by="bob")
    make_refund()

    html = client_as("carol").get("/refunds").text
    assert re.search(r'<option value="partially_approved"\s*>Awaiting second approval<', html)
    assert re.search(r'<option value="payment_unknown"\s*>Payment outcome unknown<', html)
    assert 'class="badge status-partially_approved">Awaiting second approval</span>' in html
    assert 'class="badge status-submitted">Submitted</span>' in html
    assert "Submitted or Awaiting second approval" in html
    assert "Approved &gt; 1 min, Payment failed, Payment outcome unknown" in html
    assert ">partially_approved<" not in html

    filtered = client_as("carol").get("/refunds?status=partially_approved").text
    assert (
        '<option value="partially_approved" selected>Awaiting second approval</option>' in filtered
    )
    assert f'href="/refunds/{waiting}"' in filtered
    assert "status-submitted" not in filtered.split("<tbody>")[1]


def test_detail_shows_the_label(database):
    refund_id = make_refund(status="payment_unknown")
    html = client_as("carol").get(f"/refunds/{refund_id}").text
    assert 'id="refund-status">Payment outcome unknown</span>' in html

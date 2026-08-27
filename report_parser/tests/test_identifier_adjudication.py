from __future__ import annotations

import unittest

from rk3588_report_parser.identifier_adjudication import adjudicate_identifiers
from rk3588_report_parser.identifier_models import ClassifiedCandidate, IdentifierCandidate


def linked(candidate_id, identifier_type, value, relation="same_line_right", confidence=0.99, distance=0.02, confirmed=True):
    candidate = IdentifierCandidate(
        candidate_id,
        identifier_type,
        value,
        (candidate_id,),
        (candidate_id + 100,),
        "full_span",
        relation,
        distance,
        confidence,
        candidate_id,
        (10, 10, 90, 40),
        ((110, 10, 260, 40),),
    )
    return ClassifiedCandidate(candidate, identifier_type, confirmed, () if confirmed else ("model_verification_rejected",))


class IdentifierAdjudicationTests(unittest.TestCase):
    def test_configured_primary_priority_can_prefer_exam_request(self) -> None:
        status, primary, _, _, _, _ = adjudicate_identifiers(
            [
                linked(1, "patient_id", "P20260001"),
                linked(2, "exam_request_no", "SQ20260001"),
            ],
            primary_priority=("exam_request_no", "patient_id", "inpatient_no", "outpatient_no", "visit_no", "exam_no", "medical_card_no"),
        )

        self.assertEqual(status, "accepted")
        self.assertEqual(primary.type, "exam_request_no")

    def test_verifier_rejected_alternative_retains_ocr_value(self) -> None:
        status, _, _, alternatives, review_reasons, _ = adjudicate_identifiers(
            [linked(1, "exam_request_no", "REQ12345", confirmed=False)]
        )

        self.assertEqual(status, "review_required")
        self.assertEqual(alternatives[0].value, "REQ12345")
        self.assertFalse(alternatives[0].validation_ok)
        self.assertIn("model_verification_conflict", review_reasons)

    def test_returns_all_types_and_prefers_patient_id(self) -> None:
        status, primary, identifiers, alternatives, review, rejection = adjudicate_identifiers(
            [
                linked(1, "exam_request_no", "SQ20260001"),
                linked(2, "inpatient_no", "ZY20260001"),
                linked(3, "patient_id", "P20260001"),
            ]
        )
        self.assertEqual(status, "accepted")
        self.assertEqual(primary.type, "patient_id")
        self.assertEqual([item.type for item in identifiers], ["patient_id", "inpatient_no", "exam_request_no"])
        self.assertEqual(alternatives, [])
        self.assertEqual(review, [])
        self.assertEqual(rejection, [])

    def test_near_tie_and_unknown_type_require_review(self) -> None:
        status, primary, _, alternatives, review, _ = adjudicate_identifiers(
            [
                linked(1, "exam_no", "JC20260001", confidence=0.99, distance=0.02),
                linked(2, "exam_no", "JC20260002", confidence=0.98, distance=0.03),
                linked(3, "other_medical_id", "DJ20260001"),
            ]
        )
        self.assertEqual(status, "review_required")
        self.assertEqual(primary.type, "exam_no")
        self.assertTrue(any(reason.startswith("near_tie:exam_no") for reason in review))
        self.assertIn("other_medical_id_requires_review", review)
        self.assertEqual(len(alternatives), 2)

    def test_verifier_conflict_never_becomes_accepted(self) -> None:
        status, primary, identifiers, alternatives, review, _ = adjudicate_identifiers(
            [linked(1, "patient_id", "P20260001", confirmed=False)]
        )
        self.assertEqual(status, "review_required")
        self.assertIsNone(primary)
        self.assertEqual(identifiers, [])
        self.assertEqual(len(alternatives), 1)
        self.assertIn("model_verification_conflict", review)


if __name__ == "__main__":
    unittest.main()

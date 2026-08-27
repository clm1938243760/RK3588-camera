from __future__ import annotations

import json
import unittest

from rk3588_report_parser.evidence_chat_linker import (
    EvidenceChatLinker,
    parse_confirmation,
    parse_single_field_evidence,
)
from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.prompt import ModelResponseError, build_user_prompt, parse_field_associations
from rk3588_report_parser.settings import LlmSettings


def span(span_id: int, line_id: int, text: str) -> OcrSpan:
    return OcrSpan(
        id=span_id,
        source_index=span_id,
        line_id=line_id,
        text=text,
        box=(20, line_id * 40, 300, line_id * 40 + 25),
        normalized_box=(20, line_id * 40, 300, line_id * 40 + 25),
        score=0.99,
    )


class FakeChatClient:
    choices = {
        "patient_id": ([1], [1], "after_delimiter"),
        "patient_name": ([2], [2], "after_delimiter"),
        "exam_item": ([3], [4, 5], "full_span"),
    }

    def __init__(self) -> None:
        self.calls = []

    def link(self, system_prompt, user_prompt, settings):
        payload = json.loads(user_prompt)
        self.calls.append(payload)
        if payload["task"].startswith("复核"):
            return json.dumps({"confirmed": True})
        labels, values, mode = self.choices.get(payload["field"], ([], [], "full_span"))
        option_id = next(
            (
                option["option_id"]
                for option in payload["candidate_evidence_options"]
                if option["label_span_ids"] == labels
                and option["value_span_ids"] == values
                and option["value_mode"] == mode
            ),
            0,
        )
        return json.dumps({"option_id": option_id})


class RetryChatClient(FakeChatClient):
    def __init__(self) -> None:
        super().__init__()
        self.patient_id_attempts = 0

    def link(self, system_prompt, user_prompt, settings):
        payload = json.loads(user_prompt)
        if payload["task"].startswith("复核"):
            return super().link(system_prompt, user_prompt, settings)
        if payload["field"] == "patient_id":
            self.calls.append(payload)
            self.patient_id_attempts += 1
            if self.patient_id_attempts == 1:
                return json.dumps({"option_id": 999})
            option_id = next(
                option["option_id"]
                for option in payload["candidate_evidence_options"]
                if option["label_span_ids"] == [1]
                and option["value_span_ids"] == [1]
                and option["value_mode"] == "after_delimiter"
            )
            return json.dumps({"option_id": option_id})
        return super().link(system_prompt, user_prompt, settings)


class EvidenceChatLinkerTests(unittest.TestCase):
    def test_selects_model_generated_label_and_value_ids(self) -> None:
        spans = [
            span(1, 1, "ID:60017768119"),
            span(2, 2, "Name:Alice"),
            span(3, 3, "Exam Item"),
            span(4, 4, "MRI right wrist"),
            span(5, 5, "MRI left knee"),
        ]
        client = FakeChatClient()
        settings = LlmSettings(
            endpoint="http://127.0.0.1:8010/v1/chat/completions",
            model="desktop-test",
            timeout_seconds=5,
            max_tokens=256,
        )

        response = EvidenceChatLinker(client).link("ignored", build_user_prompt(spans), settings)
        associations = parse_field_associations(response)

        self.assertEqual(associations.label_links["patient_id"], [1])
        self.assertEqual(associations.value_modes["patient_id"], "after_delimiter")
        self.assertEqual(associations.label_links["exam_item"], [3])
        self.assertEqual(associations.value_links["exam_item"], [4, 5])
        self.assertGreaterEqual(len(client.calls), 3)

    def test_rejects_free_text_and_unknown_span_ids(self) -> None:
        with self.assertRaises(ModelResponseError):
            parse_single_field_evidence(
                '{"label_span_ids":[1],"value_span_ids":[99],"value_mode":"full_span"}',
                [1, 2],
            )
        with self.assertRaises(ModelResponseError):
            parse_confirmation('{"confirmed":"yes"}')

    def test_retries_one_invalid_field_contract(self) -> None:
        spans = [
            span(1, 1, "ID:60017768119"),
            span(2, 2, "Name:Alice"),
            span(3, 3, "Exam Item"),
            span(4, 4, "MRI right wrist"),
            span(5, 5, "MRI left knee"),
        ]
        client = RetryChatClient()
        settings = LlmSettings(
            endpoint="http://127.0.0.1:8010/v1/chat/completions",
            model="desktop-test",
            timeout_seconds=5,
            max_tokens=256,
        )

        response = EvidenceChatLinker(client).link("ignored", build_user_prompt(spans), settings)
        associations = parse_field_associations(response)

        self.assertEqual(client.patient_id_attempts, 2)
        self.assertEqual(associations.label_links["patient_id"], [1])
        with self.assertRaises(ModelResponseError):
            parse_single_field_evidence(
                '{"label_span_ids":[1],"value_span_ids":["Alice"],"value_mode":"full_span"}',
                [1, 2],
            )

    def test_patient_id_target_skips_every_other_field(self) -> None:
        spans = [
            span(1, 1, "ID:60017768119"),
            span(2, 2, "Name:Alice"),
            span(3, 3, "Exam Item"),
            span(4, 4, "MRI right wrist"),
        ]
        client = FakeChatClient()
        settings = LlmSettings(
            endpoint="http://127.0.0.1:8010/v1/chat/completions",
            model="desktop-test",
            timeout_seconds=5,
            max_tokens=256,
        )

        response = EvidenceChatLinker(client, target_fields=("patient_id",)).link(
            "ignored", build_user_prompt(spans), settings
        )
        associations = parse_field_associations(response)

        field_calls = [payload["field"] for payload in client.calls if "field" in payload]
        self.assertEqual(field_calls, ["patient_id", "patient_id"])
        self.assertEqual(associations.value_links["patient_id"], [1])
        self.assertEqual(associations.value_links["patient_name"], [])


if __name__ == "__main__":
    unittest.main()

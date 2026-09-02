import contextlib
import io
import json
import os
import queue
import re
import tempfile
import time
import unittest

import app
from app import Translator


class FakeTranslator(Translator):
    def __init__(self, **config):
        super().__init__({"context_size": 65536, **config})
        self.prompts = []
        self.sources = []

    @staticmethod
    def translated(source):
        return "译：" + source

    def _complete_prompt(
        self, prompt, max_tokens, on_stream=None, retry_on_length=True,
    ):
        self.prompts.append(prompt)
        match = re.search(r"【当前待译文本（.*?）】\n(.*)\Z", prompt, re.DOTALL)
        if not match:
            return False, "测试替身无法定位当前待译文本"
        source = match.group(1)
        self.sources.append(source)
        output = self.translated(source)
        if on_stream:
            on_stream("")
            on_stream(output[:2])
            on_stream(output)
        return True, output


class TranslationFlowTests(unittest.TestCase):
    def test_model_http_connection_is_closed_after_every_response(self):
        class FakeResponse:
            status = 200

            @staticmethod
            def getheader(name, default=None):
                if name == "Content-Type":
                    return "application/json"
                return default

            @staticmethod
            def read():
                return json.dumps({
                    "choices": [{
                        "message": {"content": "done"},
                        "finish_reason": "stop",
                    }]
                }).encode("utf-8")

        class FakeConnection:
            def __init__(self):
                self.headers = None
                self.closed = False

            def request(self, _method, _path, body=None, headers=None):
                self.headers = headers

            @staticmethod
            def getresponse():
                return FakeResponse()

            def close(self):
                self.closed = True

        translator = Translator({})
        connection = FakeConnection()
        translator._conn = connection

        result = translator._post_json({"stream": False})

        self.assertEqual(result["choices"][0]["message"]["content"], "done")
        self.assertEqual(connection.headers["Connection"], "close")
        self.assertTrue(connection.closed)
        self.assertIsNone(translator._conn)

    def test_each_physical_position_is_a_separate_request(self):
        translator = FakeTranslator()
        emitted = []
        progress = []
        checkpoints = []
        content = '# Chapter\nFirst line.\n\n- Second "quoted" line.\n> Third line.'

        ok, output = translator.translate_content(
            content,
            emit=lambda line, text: emitted.append((line, text)),
            progress=lambda done, total: progress.append((done, total)),
            checkpoint=lambda line, text: checkpoints.append((line, text)),
        )

        self.assertTrue(ok, output)
        self.assertEqual(
            translator.sources,
            ["Chapter", "First line.", 'Second "quoted" line.', "Third line."],
        )
        self.assertEqual(
            output,
            '# 译：Chapter\n译：First line.\n\n- 译：Second "quoted" line.\n> 译：Third line.',
        )
        self.assertTrue(
            all("【固定全文原文参考" in prompt for prompt in translator.prompts)
        )
        self.assertIn("【最近已确认译文", translator.prompts[1])
        self.assertIn("[原文]\nChapter\n[译文]\n译：Chapter", translator.prompts[1])
        self.assertEqual(progress[0], (0, 4))
        self.assertEqual(progress[-1], (4, 4))
        self.assertEqual([line for line, _ in checkpoints], [0, 1, 3, 4])
        self.assertIn((3, '- 译：Second "quoted" line.'), emitted)

    def test_resume_skips_completed_position(self):
        translator = FakeTranslator()
        checkpoints = []

        ok, output = translator.translate_content(
            "One.\nTwo.\nThree.",
            resume_lines={"0": "已完成：One."},
            checkpoint=lambda line, text: checkpoints.append((line, text)),
        )

        self.assertTrue(ok, output)
        self.assertEqual(translator.sources, ["Two.", "Three."])
        self.assertEqual(output, "已完成：One.\n译：Two.\n译：Three.")
        self.assertEqual([line for line, _ in checkpoints], [0, 1, 2])

    def test_whole_file_retranslation_does_not_anchor_to_old_draft(self):
        translator = FakeTranslator()

        ok, output = translator.translate_content(
            "One.\nTwo.",
            previous_content="OLD ONE\nOLD TWO",
        )

        self.assertTrue(ok, output)
        self.assertEqual(output, "译：One.\n译：Two.")
        joined_prompts = "\n".join(translator.prompts)
        self.assertNotIn("OLD ONE", joined_prompts)
        self.assertNotIn("OLD TWO", joined_prompts)
        self.assertIn("这是重译任务", joined_prompts)

    def test_context_uses_complete_neighbor_units_and_configured_counts(self):
        translator = Translator({
            "context_size": 65536,
            "context_units": 3,
            "future_context_units": 2,
        })
        history = [
            (f"Source {index}.", f"译文 {index}。")
            for index in range(6)
        ]
        future = [f"Future {index}." for index in range(5)]

        before = translator._context_text(history)
        after = translator._future_context_text(future)

        self.assertNotIn("Source 2.", before)
        self.assertIn("Source 3.", before)
        self.assertIn("Source 5.", before)
        self.assertEqual(after, "Future 0.\n\nFuture 1.")
        self.assertNotIn("Future 2.", after)

    def test_neighbor_mode_uses_only_nearby_context(self):
        translator = FakeTranslator(
            context_mode="neighbor",
            context_units=1,
            future_context_units=1,
        )

        ok, output = translator.translate_content("One.\nTwo.\nThree.")

        self.assertTrue(ok, output)
        self.assertEqual(output, "译：One.\n译：Two.\n译：Three.")
        self.assertTrue(
            all("【固定" not in prompt for prompt in translator.prompts)
        )
        self.assertIn("【邻近下文参考", translator.prompts[0])
        self.assertIn("Two.", translator.prompts[0])
        self.assertIn("【最近已确认译文", translator.prompts[1])
        self.assertIn("[原文]\nOne.\n[译文]\n译：One.", translator.prompts[1])
        self.assertNotIn("Three.", translator.prompts[0])

    def test_unit_mode_sends_only_the_current_unit(self):
        translator = FakeTranslator(
            context_mode="unit",
            context_units=12,
            future_context_units=6,
        )

        ok, output = translator.translate_content("One.\nTwo.\nThree.")

        self.assertTrue(ok, output)
        self.assertEqual(output, "译：One.\n译：Two.\n译：Three.")
        for source, prompt in zip(translator.sources, translator.prompts):
            self.assertNotIn("【固定", prompt)
            self.assertNotIn("【最近已确认译文", prompt)
            self.assertNotIn("【邻近下文参考", prompt)
            self.assertTrue(prompt.endswith(source))

    def test_invalid_context_mode_falls_back_to_full(self):
        translator = FakeTranslator(context_mode="unknown")

        ok, output = translator.translate_content("One.\nTwo.")

        self.assertTrue(ok, output)
        self.assertTrue(
            all("【固定全文原文参考" in prompt for prompt in translator.prompts)
        )

    def test_srt_translates_only_caption_text(self):
        translator = FakeTranslator(context_mode="full")
        source = (
            "1\n00:00:01,000 --> 00:00:03,500\nHello there.\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\nHow are you?"
        )

        ok, output = translator.translate_content(source, "sample.srt")

        self.assertTrue(ok, output)
        self.assertEqual(translator.sources, ["Hello there.", "How are you?"])
        self.assertEqual(
            output,
            "1\n00:00:01,000 --> 00:00:03,500\n译：Hello there.\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n译：How are you?",
        )
        joined_prompts = "\n".join(translator.prompts)
        self.assertNotIn("00:00:01,000", joined_prompts)
        self.assertNotIn("\n1\n", joined_prompts)

    def test_srt_bom_and_crlf_are_normalized_before_unit_detection(self):
        translator = FakeTranslator(context_mode="unit")
        source = (
            "\ufeff1\r\n00:00:01,000 --> 00:00:03,500\r\nHello there.\r\n"
        )

        ok, output = translator.translate_content(source, "sample.srt")

        self.assertTrue(ok, output)
        self.assertEqual(translator.sources, ["Hello there."])
        self.assertEqual(
            output,
            "1\n00:00:01,000 --> 00:00:03,500\n译：Hello there.\n",
        )

    def test_webvtt_preserves_header_cue_ids_and_control_blocks(self):
        translator = FakeTranslator(context_mode="unit")
        source = (
            "WEBVTT - Example\nLanguage: en\n\n"
            "intro\n00:01.000 --> 00:03.000 position:10%\nWelcome.\n\n"
            "NOTE internal comment\nDo not translate this.\n\n"
            "00:04.000 --> 00:06.000\nContinue."
        )

        ok, output = translator.translate_content(source, "sample.vtt")

        self.assertTrue(ok, output)
        self.assertEqual(translator.sources, ["Welcome.", "Continue."])
        self.assertIn("Language: en", output)
        self.assertIn("intro\n00:01.000 --> 00:03.000 position:10%", output)
        self.assertIn("NOTE internal comment\nDo not translate this.", output)
        self.assertIn("译：Welcome.", output)
        self.assertTrue(output.endswith("译：Continue."))

    def test_subtitle_line_retranslation_respects_format_units(self):
        translator = FakeTranslator(context_mode="unit")
        source = "1\n00:00:01,000 --> 00:00:03,500\nHello there."
        previous = "1\n00:00:01,000 --> 00:00:03,500\n旧译文"

        ok, result = translator.retranslate_line(
            source, previous, 2, "sample.srt"
        )

        self.assertTrue(ok, result)
        output_line, updated = result
        self.assertEqual(output_line, "译：Hello there.")
        self.assertEqual(
            updated,
            "1\n00:00:01,000 --> 00:00:03,500\n译：Hello there.",
        )
        with self.assertRaisesRegex(ValueError, "不能单独重译"):
            translator.retranslate_line(source, updated, 1, "sample.srt")

    def test_document_reference_dynamically_selects_full_text_or_windows(self):
        small = Translator({"context_size": 65536})
        small_content = "First paragraph.\n\nSecond paragraph."
        small_items = [(0, "First paragraph."), (2, "Second paragraph.")]
        small_plan = small._document_reference_plan(small_content, small_items)

        self.assertTrue(
            all(entry["kind"] == "全文原文参考" for entry in small_plan)
        )
        self.assertTrue(all(entry["text"] == small_content for entry in small_plan))

        large = Translator({"context_size": 2048})
        large_content = "\n\n".join(
            f"Paragraph {index}: " + ("context words " * 35)
            for index in range(30)
        )
        lines = large_content.split("\n")
        large_items = [
            (index, line) for index, line in enumerate(lines) if line.strip()
        ]
        large_plan = large._document_reference_plan(large_content, large_items)
        windows = [entry["window"] for entry in large_plan]

        self.assertGreater(len(set(windows)), 1)
        self.assertTrue(
            all(entry["kind"] == "当前语义窗口参考" for entry in large_plan)
        )
        for window in set(windows):
            references = {
                entry["text"] for entry in large_plan if entry["window"] == window
            }
            self.assertEqual(len(references), 1)

    def test_terminal_log_contains_request_and_response_but_not_api_key(self):
        translator = Translator({
            "api_key": "private-test-api-key",
            "max_retries": 1,
            "src_lang": "英文",
            "tgt_lang": "中文",
        })

        sent_payload = {}

        def fake_post(payload, on_delta=None):
            sent_payload.update(payload)
            if on_delta:
                on_delta("测试回复", "测试回复")
            return {
                "choices": [{
                    "message": {"content": "测试回复"},
                    "finish_reason": "stop",
                }]
            }

        translator._post_json = fake_post
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            ok, output = translator.translate_text("Source sentence.")

        log = captured.getvalue()
        self.assertTrue(ok, output)
        self.assertEqual(output, "测试回复")
        self.assertIn("模型请求 #", log)
        self.assertIn("Source sentence.", log)
        self.assertIn("模型回复 #", log)
        self.assertIn("测试回复", log)
        self.assertNotIn("private-test-api-key", log)
        self.assertFalse(sent_payload["chat_template_kwargs"]["enable_thinking"])

    def test_text_output_restores_original_bom_and_crlf(self):
        original_paths = (app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH)
        with tempfile.TemporaryDirectory() as root:
            try:
                app.OUTPUTS_DIR = os.path.join(root, "outputs")
                app.OUTPUT_INDEX_PATH = os.path.join(root, "state.json")
                output_name = app.save_translation_output(
                    {
                        "name": "captions.srt",
                        "content": "1\n00:00:01,000 --> 00:00:02,000\nHello",
                        "source_format": "text",
                        "text_eol": "crlf",
                        "text_bom": True,
                    },
                    "1\n00:00:01,000 --> 00:00:02,000\n你好",
                    "中文",
                )
                output_path = os.path.join(app.OUTPUTS_DIR, output_name)
                with open(output_path, "rb") as handle:
                    raw = handle.read()

                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
                self.assertIn(b"\r\n", raw)
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
                self.assertEqual(
                    app.read_output_preview(output_path, output_name),
                    "1\n00:00:01,000 --> 00:00:02,000\n你好",
                )
            finally:
                app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH = original_paths

    def test_sse_queue_is_bounded_and_keeps_recent_events(self):
        job = {"q": queue.Queue(maxsize=2)}
        for index in range(5):
            app.enqueue_job_event(job, {"kind": "chunk", "index": index})

        self.assertEqual(job["q"].qsize(), 2)
        self.assertEqual(job["q"].get_nowait()["index"], 3)
        self.assertEqual(job["q"].get_nowait()["index"], 4)

    def test_status_snapshot_is_detached_from_mutable_job_state(self):
        job_id = "snapshot-test"
        job = {
            "status": "running",
            "total": 1,
            "done": 0,
            "current": "sample.txt",
            "results": [],
            "files": [{"name": "sample.txt", "content": "Source"}],
            "done_names": [],
            "partials": {"sample.txt": {"0": "译文"}},
            "completed_partials": {"sample.txt": {"0": "译文"}},
        }
        with app.JOBS_LOCK:
            app.JOBS[job_id] = job
        try:
            snapshot = app.job_status_snapshot(job_id, requested_full=True)
            job["partials"]["sample.txt"]["0"] = "已变更"
            job["files"][0]["content"] = "Changed"

            self.assertEqual(snapshot["partials"]["sample.txt"]["0"], "译文")
            self.assertEqual(snapshot["sources"][0]["content"], "Source")
        finally:
            with app.JOBS_LOCK:
                app.JOBS.pop(job_id, None)

    def test_finished_job_pruning_applies_global_text_budget(self):
        original_budget = app.MAX_FINISHED_JOB_TEXT_CHARS
        with app.JOBS_LOCK:
            original_jobs = dict(app.JOBS)
            app.JOBS.clear()
            now = time.time()
            app.JOBS.update({
                "newest": {
                    "status": "done", "finished_at": now,
                    "results": [{"content": "n" * 10}],
                },
                "older": {
                    "status": "done", "finished_at": now - 1,
                    "results": [{"content": "o" * 10}],
                },
            })
        try:
            app.MAX_FINISHED_JOB_TEXT_CHARS = 12
            app.prune_jobs()
            with app.JOBS_LOCK:
                self.assertIn("newest", app.JOBS)
                self.assertNotIn("older", app.JOBS)
        finally:
            app.MAX_FINISHED_JOB_TEXT_CHARS = original_budget
            with app.JOBS_LOCK:
                app.JOBS.clear()
                app.JOBS.update(original_jobs)


if __name__ == "__main__":
    unittest.main()

"""outcome_from_history must not report a result unless the agent called `done`."""

from browser_mcp.runner import outcome_from_history


class FakeHistory:
    def __init__(self, done, success, final, errors, urls, steps):
        self._done, self._success, self._final = done, success, final
        self._errors, self._urls, self._steps = errors, urls, steps

    def is_done(self):
        return self._done

    def is_successful(self):
        return self._success if self._done else None

    def final_result(self):
        return self._final

    def errors(self):
        return self._errors

    def urls(self):
        return self._urls

    def number_of_steps(self):
        return self._steps


def test_done_success():
    urls = ["about:blank", "https://a", "https://a"]
    history = FakeHistory(True, True, "Title: Example", [None, None], urls, 2)
    outcome = outcome_from_history(history)
    assert outcome.done and outcome.success
    assert outcome.result == "Title: Example"
    assert outcome.visited_urls == ["https://a"]
    assert outcome.error is None


def test_step_limit_hides_partial_content():
    errors = [None, "Failed to complete task in maximum steps"]
    history = FakeHistory(False, None, "some extracted text", errors, [], 40)
    outcome = outcome_from_history(history)
    assert not outcome.done
    assert outcome.result is None
    assert outcome.error == "Failed to complete task in maximum steps"


def test_done_but_unsuccessful():
    history = FakeHistory(True, False, "Blocked by captcha", [], [], 5)
    outcome = outcome_from_history(history)
    assert outcome.done and outcome.success is False
    assert outcome.result == "Blocked by captcha"
    assert outcome.error

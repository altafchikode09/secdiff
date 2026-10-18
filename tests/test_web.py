"""Web layer tests — all offline.

The live route is exercised by monkeypatching the shared pipeline entry
point the web module calls (secdiff.web.live_pipeline); the real EDGAR
path is verified live against the running server, not by unit tests.
There is no demo route or demo content on the website by design: every
report the site produces is generated live from EDGAR at request time.
"""
import io

import pytest

import secdiff.web as web
from secdiff.web import application, parse_report_form


# -- form parsing -----------------------------------------------------------

def test_parse_minimal_form():
    p = parse_report_form("ticker=aapl")
    assert p.ticker == "AAPL"
    assert p.form == "10-K"
    assert p.risk_only is False and p.deletions_only is False
    assert p.top is None


def test_parse_full_form():
    p = parse_report_form(
        "ticker=brk.b&form=10-Q&risk_only=on&deletions_only=on&top=25")
    assert p.ticker == "BRK.B"
    assert p.form == "10-Q"
    assert p.risk_only is True and p.deletions_only is True
    assert p.top == 25


def test_parse_demo_word_is_an_ordinary_ticker():
    # "demo" has no special meaning on the website: it is parsed as an
    # ordinary ticker string and would be looked up on EDGAR like any
    # other (and rejected there if no such ticker exists).
    p = parse_report_form("ticker=demo")
    assert p.ticker == "DEMO"


def test_parse_company_name_is_accepted():
    # Company names are valid input now (resolution maps them to a ticker
    # and says so); spaces are collapsed and the value is upper-cased.
    p = parse_report_form("ticker=Berkshire++Hathaway")
    assert p.ticker == "BERKSHIRE HATHAWAY"


@pytest.mark.parametrize("body", [
    "",                     # missing ticker
    "ticker=",              # blank ticker
    "ticker=AAPL&form=8-K",  # unsupported form
    "ticker=AAPL&top=abc",  # non-numeric top
    "ticker=AAPL&top=0",    # out-of-range top
    "ticker=AAPL&top=9999",  # out-of-range top
])
def test_parse_rejects_bad_input(body):
    with pytest.raises(ValueError):
        parse_report_form(body)


# -- WSGI application ---------------------------------------------------------

def call_app(method, path, query="", body=b"", cookie=""):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_COOKIE": cookie,
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = application(environ, start_response)
    text = b"".join(chunks).decode("utf-8")
    return captured["status"], text


def call_app_h(method, path, query="", body=b"", cookie=""):
    """Like call_app but also returns the response headers dict."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_COOKIE": cookie,
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = application(environ, start_response)
    text = b"".join(chunks).decode("utf-8")
    return captured["status"], captured["headers"], text


def stub_resolution(monkeypatch, tmp_path, mapping):
    """Stub EDGAR resolution + usage store for /report tests.

    mapping: ticker -> CIK int, or ticker -> exception instance to raise.
    Returns the UsageStore in use so tests can inspect consumption."""
    def fake_resolve(params):
        hit = mapping[params.ticker]
        if isinstance(hit, BaseException):   # SystemExit is a BaseException
            raise hit
        return hit, f"Company {params.ticker}"

    store = web.UsageStore(tmp_path / "usage.json")
    monkeypatch.setattr(web, "_resolve_company", fake_resolve)
    monkeypatch.setattr(web, "_STORE", store)
    return store


def test_home_page():
    status, text = call_app("GET", "/")
    assert status == "200 OK"
    assert 'name="ticker"' in text
    assert 'name="form"' in text and "10-Q" in text
    assert 'name="risk_only"' in text and 'name="deletions_only"' in text
    assert 'name="top"' in text
    assert 'id="loading"' in text            # loading state
    assert "How it works" in text
    assert "Not investment advice" in text   # footer disclaimer


def test_home_page_has_no_demo_anywhere():
    _status, text = call_app("GET", "/")
    assert "/demo" not in text
    assert "demo" not in text.lower()


def test_demo_path_does_not_exist():
    status, _text = call_app("GET", "/demo")
    assert status.startswith("404")


def test_healthz():
    status, text = call_app("GET", "/healthz")
    assert status == "200 OK" and text.strip() == "ok"


def _fake_pipeline_result(ticker):
    """A pipeline result shaped exactly like a real live_pipeline return,
    so render_reports/render_html run for real in this test."""
    from secdiff.diffing import Hunk, SectionDiff
    from secdiff.report import FilingInfo
    from secdiff.xbrl import XbrlResult

    info = FilingInfo(
        ticker=ticker, company="Example Corp", form="10-K", cik=12345,
        new_accession="0000012345-26-000001", new_date="2026-02-01",
        new_url="https://www.sec.gov/example-new",
        new_index_url="https://www.sec.gov/example-new-index",
        old_accession="0000012345-25-000001", old_date="2025-02-01",
        old_url="https://www.sec.gov/example-old",
        old_index_url="https://www.sec.gov/example-old-index",
        section_confidence="high")
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk(kind="deletion", old="A sentence that was removed.", size=10),
        Hunk(kind="addition", new="A sentence that was added.", size=10),
    ])]
    xbrl = XbrlResult(rows=[], unmapped=[], matched=0, considered=100)
    return info, diffs, xbrl


def test_report_route_uses_live_pipeline(monkeypatch, tmp_path):
    calls = []

    def fake_live(args):
        calls.append(args.ticker)
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    stub_resolution(monkeypatch, tmp_path, {"EXMP": 12345})
    status, text = call_app("POST", "/report",
                            body=b"ticker=EXMP&form=10-K&risk_only=on")
    assert status == "200 OK"
    assert calls == ["EXMP"]                  # the live pipeline was used
    assert "1 deletions" in text and "1 additions" in text
    assert "0000012345-26-000001" in text     # accession from the pipeline
    assert "New secdiff search" in text       # web chrome added


def test_report_unknown_ticker_shows_honest_error(monkeypatch, tmp_path):
    store = stub_resolution(
        monkeypatch, tmp_path,
        {"ZZZZZ": SystemExit("Ticker not found on EDGAR: ZZZZZ")})
    status, text = call_app("POST", "/report", body=b"ticker=ZZZZZ")
    assert status.startswith("400")
    assert "Ticker not found on EDGAR: ZZZZZ" in text
    assert "No report was generated" in text
    # A failed resolution must not consume a free slot.
    assert store._load() == {}


def test_report_edgar_unreachable_shows_honest_error(monkeypatch, tmp_path):
    import urllib.error

    def fake_live(args):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    stub_resolution(monkeypatch, tmp_path, {"AAPL": 320193})
    status, text = call_app("POST", "/report", body=b"ticker=AAPL")
    assert status.startswith("502")
    assert "SEC EDGAR could not be reached" in text


def test_report_bad_input_is_400():
    status, text = call_app("POST", "/report", body=b"ticker=")
    assert status.startswith("400")
    assert "Please enter a ticker symbol" in text


def test_unknown_path_is_404():
    status, text = call_app("GET", "/nope")
    assert status.startswith("404")
    assert "Page not found" in text


# -- free daily limit + pricing ----------------------------------------------

def test_usage_store_counts_distinct_companies(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for cik in (1, 2, 3, 4):
        assert store.check_and_add("v1", cik, day="2026-10-17") is True
    assert store.check_and_add("v1", 5, day="2026-10-17") is False
    # A different visitor has their own allowance.
    assert store.check_and_add("v2", 5, day="2026-10-17") is True


def test_usage_store_repeat_company_is_free(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for _ in range(6):
        assert store.check_and_add("v1", 42, day="2026-10-17") is True
    assert store.companies_today("v1", day="2026-10-17") == [42]


def test_usage_store_day_rollover_resets(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for cik in (1, 2, 3, 4):
        assert store.check_and_add("v1", cik, day="2026-10-17") is True
    assert store.check_and_add("v1", 5, day="2026-10-17") is False
    assert store.check_and_add("v1", 5, day="2026-10-18") is True
    assert store.companies_today("v1", day="2026-10-18") == [5]


def test_usage_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("not json {{{", "utf-8")
    store = web.UsageStore(path)
    assert store.check_and_add("v1", 7, day="2026-10-17") is True
    assert store.companies_today("v1", day="2026-10-17") == [7]


def test_home_page_shows_pricing_and_waitlist():
    status, text = call_app("GET", "/")
    assert status == "200 OK"
    assert "$9" in text and "$27" in text
    assert "4 companies per day" in text
    assert 'action="/waitlist"' in text
    assert "Payments opening soon" in text


def test_visitor_cookie_is_set_and_reused():
    status, headers, _text = call_app_h("GET", "/")
    assert status == "200 OK"
    assert headers["Set-Cookie"].startswith("secdiff_vid=")
    assert "HttpOnly" in headers["Set-Cookie"]
    vid = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    # Presenting the cookie back must not mint a new one.
    _s, headers2, _t = call_app_h("GET", "/", cookie=f"secdiff_vid={vid}")
    assert "Set-Cookie" not in headers2


def test_fifth_distinct_company_gets_paywall(monkeypatch, tmp_path):
    def fake_live(args):
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    mapping = {t: 1000 + i for i, t in
               enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])}
    stub_resolution(monkeypatch, tmp_path, mapping)
    cookie = "secdiff_vid=" + "ab" * 16
    for t in ("AAA", "BBB", "CCC", "DDD"):
        status, _text = call_app("POST", "/report",
                                 body=f"ticker={t}".encode(), cookie=cookie)
        assert status == "200 OK", t
    # 5th distinct company -> paywall with both paid plans
    status, text = call_app("POST", "/report", body=b"ticker=EEE",
                            cookie=cookie)
    assert status.startswith("402")
    assert "$9" in text and "$27" in text
    assert "4 free companies" in text and "reset tomorrow" in text
    # Re-viewing an already-counted company still works at the limit.
    status, _text = call_app("POST", "/report", body=b"ticker=AAA",
                             cookie=cookie)
    assert status == "200 OK"


def call_raw(method, path, query="", body=b"", cookie=""):
    """Like call_app but returns the raw (status, headers-list, text) so
    tests can see multiple Set-Cookie headers."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_COOKIE": cookie,
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    chunks = application(environ, start_response)
    text = b"".join(chunks).decode("utf-8")
    return captured["status"], captured["headers"], text


def _set_cookie_value(headers, name):
    for k, v in headers:
        if k == "Set-Cookie" and v.startswith(name + "="):
            return v.split(";")[0].split("=", 1)[1]
    return None


@pytest.fixture()
def accounts_env(monkeypatch, tmp_path):
    """Isolated account store + usage store + capturing mail stub.

    Returns (store, sent) where sent collects (to, subject, body) of
    every email the mailer was asked to send."""
    import re as _re

    import secdiff.accounts as accounts_mod

    store = web.AccountStore(tmp_path / "secdiff.db")
    monkeypatch.setattr(web, "_ACCOUNTS", store)
    monkeypatch.setattr(web, "_STORE", web.UsageStore(tmp_path / "u.json"))
    sent = []

    def fake_send(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(accounts_mod, "send_email", fake_send)

    def last_code():
        m = _re.search(r"is: (\d{6})", sent[-1][2])
        return m.group(1)

    return store, sent, last_code


def _signup(body=b"name=Test+User&email=test%40example.com"
            b"&username=testuser&password=password123"):
    return call_raw("POST", "/signup", body=body)


def test_signup_happy_path_logs_you_in(accounts_env):
    status, headers, _text = _signup()
    assert status.startswith("303")
    token = _set_cookie_value(headers, "secdiff_session")
    assert token
    status, text = call_app("GET", "/", cookie=f"secdiff_session={token}")
    assert status == "200 OK"
    assert "Hi, <strong>testuser</strong>" in text
    assert "My account" in text


def test_signup_duplicate_username_and_email_rejected(accounts_env):
    _signup()
    status, _h, text = _signup(
        b"email=other%40example.com&username=testuser"
        b"&password=password123")
    assert status.startswith("400")
    assert "username is already taken" in text
    status, _h, text = _signup(
        b"email=test%40example.com&username=someoneelse"
        b"&password=password123")
    assert status.startswith("400")
    assert "already exists" in text


def test_signup_validation_errors(accounts_env):
    status, _h, text = _signup(
        b"email=test%40example.com&username=ab&password=password123")
    assert status.startswith("400") and "3–30 characters" in text
    status, _h, text = _signup(
        b"email=not-an-email&username=goodname&password=password123")
    assert status.startswith("400") and "email" in text
    status, _h, text = _signup(
        b"email=test%40example.com&username=goodname&password=short")
    assert status.startswith("400") and "at least 8" in text


def test_login_success_and_failure(accounts_env):
    _signup()
    # Wrong password: generic error, no hint which part failed.
    status, text = call_app(
        "POST", "/login", body=b"identifier=testuser&password=wrongpass1")
    assert status.startswith("401")
    assert "don't match our records" in text
    # Unknown user: same message.
    status, text = call_app(
        "POST", "/login", body=b"identifier=nobody&password=password123")
    assert status.startswith("401")
    assert "don't match our records" in text
    # Correct login by username and by email both work.
    for ident in (b"identifier=testuser&password=password123",
                  b"identifier=test%40example.com&password=password123"):
        status, headers, _t = call_raw("POST", "/login", body=ident)
        assert status.startswith("303")
        assert _set_cookie_value(headers, "secdiff_session")


def test_session_persistence_and_logout(accounts_env):
    _signup()
    _s, headers, _t = call_raw(
        "POST", "/login", body=b"identifier=testuser&password=password123")
    token = _set_cookie_value(headers, "secdiff_session")
    cookie = f"secdiff_session={token}"
    status, text = call_app("GET", "/account", cookie=cookie)
    assert status == "200 OK"
    assert "testuser" in text and "test@example.com" in text
    assert "0 of 4 free companies used" in text
    call_app("POST", "/logout", cookie=cookie)
    status, headers, _t = call_raw("GET", "/account", cookie=cookie)
    assert status.startswith("303")            # bounced to /login
    assert dict(headers).get("Location") == "/login"


def test_logged_in_report_limit_is_account_keyed(accounts_env, monkeypatch,
                                                 tmp_path):
    def fake_live(args):
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    mapping = {t: 2000 + i for i, t in
               enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])}
    # stub_resolution installs the usage store that both /report and
    # /account read via web._STORE, so the counters line up.
    stub_resolution(monkeypatch, tmp_path, mapping)
    _signup()
    _s, headers, _t = call_raw(
        "POST", "/login", body=b"identifier=testuser&password=password123")
    token = _set_cookie_value(headers, "secdiff_session")
    cookie = f"secdiff_session={token}"
    for t in ("AAA", "BBB", "CCC", "DDD"):
        status, _text = call_app("POST", "/report",
                                 body=f"ticker={t}".encode(), cookie=cookie)
        assert status == "200 OK", t
    status, _text = call_app("POST", "/report", body=b"ticker=EEE",
                             cookie=cookie)
    assert status.startswith("402")     # account limit is still 4
    # /account reflects the account-keyed counter.
    status, text = call_app("GET", "/account", cookie=cookie)
    assert "4 of 4 free companies used" in text


def test_change_password_flow(accounts_env):
    _signup()
    _s, headers, _t = call_raw(
        "POST", "/login", body=b"identifier=testuser&password=password123")
    token = _set_cookie_value(headers, "secdiff_session")
    cookie = f"secdiff_session={token}"
    status, text = call_app(
        "POST", "/change-password", cookie=cookie,
        body=b"current_password=nope12345&new_password=newpassword9")
    assert status.startswith("400")
    assert "current password doesn't match" in text
    status, text = call_app(
        "POST", "/change-password", cookie=cookie,
        body=b"current_password=password123&new_password=newpassword9")
    assert status == "200 OK"
    assert "password has been changed" in text
    call_app("POST", "/logout", cookie=cookie)
    status, _t = call_app(
        "POST", "/login", body=b"identifier=testuser&password=password123")
    assert status.startswith("401")     # old password dead
    _s, headers, _t = call_raw(
        "POST", "/login", body=b"identifier=testuser&password=newpassword9")
    assert _set_cookie_value(headers, "secdiff_session")


def test_forgot_password_full_cycle(accounts_env):
    store, sent, last_code = accounts_env
    _signup()
    status, text = call_app("POST", "/forgot-password",
                            body=b"step=request&identifier=testuser")
    assert status == "200 OK"
    assert "6-digit code is on its way" in text
    assert len(sent) == 1 and sent[0][0] == "test@example.com"
    code = last_code()
    # Wrong code is rejected.
    wrong = "999999" if code != "999999" else "111111"
    status, text = call_app(
        "POST", "/forgot-password",
        body=(f"step=reset&identifier=testuser&code={wrong}"
              f"&new_password=resetpass42").encode())
    assert status.startswith("400")
    # Correct code resets the password.
    status, text = call_app(
        "POST", "/forgot-password",
        body=(f"step=reset&identifier=testuser&code={code}"
              f"&new_password=resetpass42").encode())
    assert status == "200 OK"
    assert "Password changed" in text
    # Old password dead, new password works.
    status, _t = call_app(
        "POST", "/login", body=b"identifier=testuser&password=password123")
    assert status.startswith("401")
    _s, headers, _t = call_raw(
        "POST", "/login", body=b"identifier=testuser&password=resetpass42")
    assert _set_cookie_value(headers, "secdiff_session")
    # The code is single-use: replaying it fails.
    status, _t = call_app(
        "POST", "/forgot-password",
        body=(f"step=reset&identifier=testuser&code={code}"
              f"&new_password=anotherpass1").encode())
    assert status.startswith("400")


def test_forgot_password_code_attempt_limit(accounts_env):
    store, sent, last_code = accounts_env
    _signup()
    call_app("POST", "/forgot-password",
             body=b"step=request&identifier=testuser")
    code = last_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        call_app("POST", "/forgot-password",
                 body=(f"step=reset&identifier=testuser&code={wrong}"
                       f"&new_password=resetpass42").encode())
    # After 5 wrong attempts even the correct code is refused.
    status, _t = call_app(
        "POST", "/forgot-password",
        body=(f"step=reset&identifier=testuser&code={code}"
              f"&new_password=resetpass42").encode())
    assert status.startswith("400")


def test_forgot_password_expired_code(monkeypatch, tmp_path):
    import secdiff.accounts as accounts_mod

    store = web.AccountStore(tmp_path / "secdiff.db")
    monkeypatch.setattr(web, "_ACCOUNTS", store)
    sent = []

    def fake_send(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(accounts_mod, "send_email", fake_send)
    _signup()
    call_app("POST", "/forgot-password",
             body=b"step=request&identifier=testuser")
    import re as _re
    code = _re.search(r"is: (\d{6})", sent[-1][2]).group(1)
    user = store.get_by_username("testuser")
    # Backdate the code's expiry.
    with store._db() as db:
        db.execute("UPDATE reset_codes SET expires_at = ? WHERE user_id = ?",
                   ("2000-01-01T00:00:00+00:00", user["id"]))
        db.commit()
    status, _t = call_app(
        "POST", "/forgot-password",
        body=(f"step=reset&identifier=testuser&code={code}"
              f"&new_password=resetpass42").encode())
    assert status.startswith("400")


def test_forgot_username_reveals_username(accounts_env):
    store, sent, last_code = accounts_env
    _signup()
    status, text = call_app("POST", "/forgot-username",
                            body=b"step=request&email=test%40example.com")
    assert status == "200 OK"
    assert len(sent) == 1
    code = last_code()
    status, text = call_app(
        "POST", "/forgot-username",
        body=f"step=verify&email=test%40example.com&code={code}".encode())
    assert status == "200 OK"
    assert "testuser" in text
    # Wrong code reveals nothing.
    call_app("POST", "/forgot-username",
             body=b"step=request&email=test%40example.com")
    code2 = last_code()
    wrong = "000000" if code2 != "000000" else "111111"
    status, text = call_app(
        "POST", "/forgot-username",
        body=f"step=verify&email=test%40example.com&code={wrong}".encode())
    assert status.startswith("400")


def test_recovery_without_mailer_is_honest(monkeypatch, tmp_path):
    """SMTP unconfigured: the flow still responds, honestly says email
    delivery is not switched on, and never shows a code on screen."""
    import secdiff.accounts as accounts_mod

    store = web.AccountStore(tmp_path / "secdiff.db")
    monkeypatch.setattr(web, "_ACCOUNTS", store)
    monkeypatch.setattr(accounts_mod, "send_email",
                        lambda to, subject, body: False)
    _signup()
    status, text = call_app("POST", "/forgot-password",
                            body=b"step=request&identifier=testuser")
    assert status == "200 OK"
    assert "Email delivery is not switched on" in text
    assert "code is:" not in text


def test_waitlist_signup_and_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("SECDIFF_CACHE_DIR", str(tmp_path))
    status, text = call_app("POST", "/waitlist",
                            body=b"name=Test+User&email=test%40example.com")
    assert status == "200 OK"
    assert "on the waitlist" in text
    lines = (tmp_path / "waitlist.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 1 and "test@example.com" in lines[0]
    status, _text = call_app("POST", "/waitlist",
                             body=b"name=X&email=not-an-email")
    assert status.startswith("400")


# -- owner stats page -----------------------------------------------------------

@pytest.fixture()
def stats_env(monkeypatch, tmp_path):
    """Accounts + stats sharing one tmp DB; STATS_USERNAME=owner1."""
    store = web.AccountStore(tmp_path / "secdiff.db")
    monkeypatch.setattr(web, "_ACCOUNTS", store)
    monkeypatch.setattr(web, "_STATS",
                        web.StatsStore(tmp_path / "secdiff.db"))
    monkeypatch.setattr(web, "_STORE", web.UsageStore(tmp_path / "u.json"))
    monkeypatch.setenv("STATS_USERNAME", "owner1")
    monkeypatch.setenv("SECDIFF_CACHE_DIR", str(tmp_path))
    return store


def _signup_as(username, email_enc):
    body = (f"name=X&email={email_enc}&username={username}"
            f"&password=password123").encode()
    status, headers, _t = call_raw("POST", "/signup", body=body)
    assert status.startswith("303"), status
    token = _set_cookie_value(headers, "secdiff_session")
    assert token
    return f"secdiff_session={token}"


def test_stats_store_event_increments(tmp_path):
    store = web.StatsStore(tmp_path / "secdiff.db")
    store.record("pageview")
    store.record("pageview")
    store.record("report", "AAPL")
    store.record("report", "AAPL")
    store.record("report", "MSFT")
    store.record("signup")
    assert store.totals() == {"visits": 0, "pageviews": 2,
                               "reports": 3, "signups": 1}
    assert store.top_companies() == [("AAPL", 2), ("MSFT", 1)]
    today = store.daily(1)[0]
    assert today["day"] == web._today_utc()
    assert today["pageviews"] == 2 and today["reports"] == 3
    assert today["signups"] == 1 and today["visits"] == 0
    assert len(store.daily(14)) == 14     # a row per day, empties included


def test_stats_store_unique_visitor_per_day(tmp_path):
    from datetime import date, timedelta

    store = web.StatsStore(tmp_path / "secdiff.db")
    today = date.fromisoformat(web._today_utc())
    yesterday = (today - timedelta(days=1)).isoformat()
    today_s = today.isoformat()
    for _ in range(3):
        store.record_visit("visitor-one", day=today_s)
    store.record_visit("visitor-two", day=today_s)
    store.record_visit("visitor-one", day=yesterday)   # new day, counts again
    assert store.totals()["visits"] == 3
    rows = {r["day"]: r for r in store.daily(2)}
    assert rows[today_s]["visits"] == 2
    assert rows[yesterday]["visits"] == 1
    # The raw cookie value is never stored — only one-way day hashes.
    with store._db() as db:
        hashes = [r["vhash"] for r in
                  db.execute("SELECT vhash FROM stats_seen").fetchall()]
    assert hashes and all("visitor" not in h for h in hashes)


def test_stats_404_for_anonymous(stats_env):
    status, text = call_app("GET", "/stats")
    assert status.startswith("404")
    assert "Page not found" in text


def test_stats_404_for_non_owner(stats_env):
    cookie = _signup_as("someoneelse", "other%40example.com")
    status, text = call_app("GET", "/stats", cookie=cookie)
    assert status.startswith("404")
    assert "Site stats" not in text


def test_stats_404_when_env_unset(stats_env, monkeypatch):
    monkeypatch.delenv("STATS_USERNAME")
    cookie = _signup_as("owner1", "owner%40example.com")
    status, _text = call_app("GET", "/stats", cookie=cookie)
    assert status.startswith("404")     # exists for no one


def test_stats_owner_sees_numbers(stats_env, monkeypatch, tmp_path):
    def fake_live(args):
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    stub_resolution(monkeypatch, tmp_path, {"AAPL": 320193, "MSFT": 789019})
    vid_a, vid_b = "aa" * 16, "bb" * 16
    for _ in range(3):
        call_app("GET", "/", cookie=f"secdiff_vid={vid_a}")
    call_app("GET", "/", cookie=f"secdiff_vid={vid_b}")
    owner_cookie = _signup_as("owner1", "owner%40example.com")
    for t in ("AAPL", "MSFT"):
        status, _text = call_app("POST", "/report",
                                 body=f"ticker={t}".encode(),
                                 cookie=owner_cookie)
        assert status == "200 OK", t
    status, text = call_app("GET", "/stats", cookie=owner_cookie)
    assert status == "200 OK"
    assert "Site stats" in text
    today = web._today_utc()
    # date, unique visitors, pageviews, reports, signups — in that order
    assert (f"<tr><td>{today}</td><td>2</td><td>4</td>"
            f"<td>2</td><td>1</td></tr>") in text
    assert "<td>AAPL</td><td>1</td>" in text
    assert "<td>MSFT</td><td>1</td>" in text
    assert "<td>Accounts</td><td>1</td>" in text
    assert "<td>Waitlist entries</td><td>0</td>" in text


def test_stats_counting_failure_never_breaks_pages(stats_env, monkeypatch):
    def boom():
        raise RuntimeError("stats DB is on fire")

    monkeypatch.setattr(web, "get_stats_store", boom)
    status, text = call_app("GET", "/")
    assert status == "200 OK"
    assert 'name="ticker"' in text
    # Signup recording fails silently too — the account is still created.
    cookie = _signup_as("unlucky", "unlucky%40example.com")
    status, text = call_app("GET", "/account", cookie=cookie)
    assert status == "200 OK"

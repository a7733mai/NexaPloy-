"""Pure support-ticket logic (no frappe import; tested in tests/test_support.py).

* SLA: the first staff response is due `FIRST_RESPONSE_HOURS[priority]` (calendar hours)
  after the ticket was opened.
* Status machine (`next_status`):

    event            from                                        to
    customer_reply   Open                                        Open (still unanswered)
                     Waiting on Customer / Support, Resolved     Waiting on Support
    staff_reply      Open, Waiting on Customer / Support,        Waiting on Customer
                     Resolved
    resolve          Open, Waiting on Customer / Support         Resolved
    close            anything but Closed                         Closed
    reopen           Resolved, Closed                            Waiting on Support

  Replies to a Closed ticket are refused (reopen it first). Resolved tickets are closed
  automatically after AUTO_CLOSE_DAYS (nexaploy.support_tasks).
* `sanitize_html`: allow-list HTML sanitiser for reply messages (Text Editor content).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any

OPEN = "Open"
WAITING_ON_CUSTOMER = "Waiting on Customer"
WAITING_ON_SUPPORT = "Waiting on Support"
RESOLVED = "Resolved"
CLOSED = "Closed"
STATUSES = (OPEN, WAITING_ON_CUSTOMER, WAITING_ON_SUPPORT, RESOLVED, CLOSED)
ACTIVE_STATUSES = (OPEN, WAITING_ON_CUSTOMER, WAITING_ON_SUPPORT)

PRIORITIES = ("Low", "Normal", "High", "Urgent")
DEFAULT_PRIORITY = "Normal"
FIRST_RESPONSE_HOURS = {"Urgent": 1, "High": 4, "Normal": 12, "Low": 24}
AT_RISK_FRACTION = 0.25  # pending tickets with < 25 % of the window left are "at risk"
AUTO_CLOSE_DAYS = 7
MAX_MESSAGE_LENGTH = 20000
MAX_OPEN_TICKETS = 25  # per team, not Resolved/Closed

_TRANSITIONS = {
	"customer_reply": {OPEN: OPEN, WAITING_ON_CUSTOMER: WAITING_ON_SUPPORT, WAITING_ON_SUPPORT: WAITING_ON_SUPPORT, RESOLVED: WAITING_ON_SUPPORT},
	"staff_reply": {OPEN: WAITING_ON_CUSTOMER, WAITING_ON_CUSTOMER: WAITING_ON_CUSTOMER, WAITING_ON_SUPPORT: WAITING_ON_CUSTOMER, RESOLVED: WAITING_ON_CUSTOMER},
	"resolve": {OPEN: RESOLVED, WAITING_ON_CUSTOMER: RESOLVED, WAITING_ON_SUPPORT: RESOLVED},
	"close": {s: CLOSED for s in (OPEN, WAITING_ON_CUSTOMER, WAITING_ON_SUPPORT, RESOLVED)},
	"reopen": {RESOLVED: WAITING_ON_SUPPORT, CLOSED: WAITING_ON_SUPPORT},
}
EVENTS = tuple(_TRANSITIONS)


class TicketError(ValueError):
	"""Invalid ticket operation; the message is shown to the user."""


# --------------------------------------------------------------------------- status


def next_status(current: str | None, event: str) -> str:
	current = current or OPEN
	if event not in _TRANSITIONS:
		raise TicketError(f"Unknown ticket event {event}")
	target = _TRANSITIONS[event].get(current)
	if target is None:
		if current == CLOSED and event in ("customer_reply", "staff_reply"):
			raise TicketError("This ticket is closed. Reopen it to reply")
		raise TicketError(f"Cannot {event.replace('_', ' ')} a ticket that is {current}")
	return target


def normalize_priority(priority: Any) -> str:
	value = str(priority or DEFAULT_PRIORITY).strip().title()
	if value not in PRIORITIES:
		raise TicketError(f"Unknown priority {priority}")
	return value


def validate_subject(subject: Any) -> str:
	text = re.sub(r"\s+", " ", str(subject or "")).strip()
	if len(text) < 3:
		raise TicketError("Please enter a subject")
	return text[:140]


def can_open_ticket(open_count: int, limit: int = MAX_OPEN_TICKETS) -> bool:
	return int(open_count or 0) < limit


# --------------------------------------------------------------------------- SLA


def _dt(value: Any) -> datetime | None:
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value
	text = str(value).strip().replace("T", " ")
	text = text.split("+")[0].rstrip("Z")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	raise ValueError(f"Invalid datetime {value!r}")


def first_response_due(opened: Any, priority: Any) -> datetime:
	hours = FIRST_RESPONSE_HOURS.get(normalize_priority(priority), FIRST_RESPONSE_HOURS[DEFAULT_PRIORITY])
	return _dt(opened) + timedelta(hours=hours)


def sla_state(opened: Any, priority: Any, first_response_at: Any = None, now: Any = None) -> dict[str, Any]:
	"""{due, state, remaining_seconds}. state: met / missed (answered in time or late),
	breached (unanswered and late), at_risk (unanswered, < 25 % of the window left), pending."""
	opened = _dt(opened)
	due = first_response_due(opened, priority)
	responded = _dt(first_response_at)
	if responded is not None:
		return {"due": due, "state": "met" if responded <= due else "missed", "remaining_seconds": None}
	now = _dt(now) or datetime.utcnow()
	remaining = int((due - now).total_seconds())
	window = (due - opened).total_seconds()
	if remaining < 0:
		state = "breached"
	elif remaining < window * AT_RISK_FRACTION:
		state = "at_risk"
	else:
		state = "pending"
	return {"due": due, "state": state, "remaining_seconds": remaining}


def should_auto_close(status: str | None, resolved_at: Any, now: Any, days: int = AUTO_CLOSE_DAYS) -> bool:
	resolved_at = _dt(resolved_at)
	if status != RESOLVED or resolved_at is None:
		return False
	return _dt(now) - resolved_at >= timedelta(days=days)


# --------------------------------------------------------------------------- HTML


ALLOWED_TAGS = {
	"p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li", "blockquote", "code", "pre",
	"a", "h1", "h2", "h3", "h4", "span", "div", "hr", "table", "thead", "tbody", "tr", "th", "td",
}
VOID_TAGS = {"br", "hr"}
DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math", "head", "title", "textarea", "select", "button", "form"}
SAFE_URL_RE = re.compile(r"^(https?://|mailto:)[^\s<>\"'`]*$", re.I)


class _Sanitizer(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.out: list[str] = []
		self.stack: list[str] = []
		self.skip = 0

	def handle_starttag(self, tag, attrs):
		tag = tag.lower()
		if tag in DROP_CONTENT_TAGS:
			if tag not in VOID_TAGS:
				self.skip += 1
			return
		if self.skip or tag not in ALLOWED_TAGS:
			return
		if tag in VOID_TAGS:
			self.out.append(f"<{tag}>")
			return
		if tag == "a":
			href = next((v for k, v in attrs if k.lower() == "href" and v), "")
			href = (href or "").strip()
			if SAFE_URL_RE.match(href):
				self.out.append(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">')
			else:
				self.out.append("<a>")
		else:
			self.out.append(f"<{tag}>")
		self.stack.append(tag)

	def handle_startendtag(self, tag, attrs):
		tag = tag.lower()
		if not self.skip and tag in VOID_TAGS:
			self.out.append(f"<{tag}>")

	def handle_endtag(self, tag):
		tag = tag.lower()
		if tag in DROP_CONTENT_TAGS:
			self.skip = max(self.skip - 1, 0)
			return
		if self.skip or tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.stack:
			return
		while self.stack:
			open_tag = self.stack.pop()
			self.out.append(f"</{open_tag}>")
			if open_tag == tag:
				break

	def handle_data(self, data):
		if not self.skip:
			self.out.append(html.escape(data, quote=False))

	def result(self):
		while self.stack:
			self.out.append(f"</{self.stack.pop()}>")
		return "".join(self.out)


def sanitize_html(value: Any, max_length: int = MAX_MESSAGE_LENGTH) -> str:
	"""Allow-list sanitiser: keeps simple formatting tags, drops every attribute except a
	safe `href` (http/https/mailto) on links (which open in a new tab with rel=noopener),
	removes script/style/iframe/... including their content, escapes all text. Plain text
	(no tags) has its line breaks kept as <br>."""
	text = str(value or "")[:max_length]  # cut the input: the parser closes what is left open
	if not re.search(r"<[A-Za-z/!?]", text):
		return html.escape(text, quote=False).replace("\r\n", "\n").replace("\n", "<br>")
	parser = _Sanitizer()
	parser.feed(text)
	parser.close()
	return parser.result()


def html_to_text(value: Any, max_length: int | None = None) -> str:
	"""Readable plain text of an HTML message (for email bodies and previews)."""
	text = str(value or "")
	text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
	text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", text)
	text = re.sub(r"<[^>]+>", "", text)
	text = html.unescape(text)
	text = re.sub(r"[ \t\r\f\v]+", " ", text)
	text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
	if max_length and len(text) > max_length:
		text = text[: max_length - 1].rstrip() + "…"
	return text


def is_blank_message(value: Any) -> bool:
	return not html_to_text(value).strip()

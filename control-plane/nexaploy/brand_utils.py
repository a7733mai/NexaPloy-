"""Pure helpers for white-label brands (no frappe import; tests/test_email_branding.py).

A **Brand** (team-owned for resellers, or platform-owned) changes the portal's title, logo,
favicon, primary color and footer, and the sender name/footer of transactional e-mails. The
portal resolves it from the request host (`portal_domains`), falling back to the default
Nexaploy brand.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping

DEFAULT_PRIMARY = "#2f5bea"
DEFAULT_BRAND: dict[str, Any] = {
	"name": None,
	"title": "Nexaploy",
	"logo": None,
	"favicon": None,
	"primary_color": DEFAULT_PRIMARY,
	"support_email": None,
	"footer_text": None,
	"email_sender_name": None,
	"email_sender_address": None,
	"is_default": True,
	"portal_domain": None,
}
PUBLIC_FIELDS = ("name", "title", "logo", "favicon", "primary_color", "support_email", "footer_text",
                 "email_sender_name", "email_sender_address", "is_default")

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^(?=.{{1,253}}$)(?:{_LABEL}\.)*{_LABEL}$")
SAFE_URL_RE = re.compile(r"^(?:/[^\s\"'<>]*|https://[^\s\"'<>]+)$")
SENDER_NAME_BAD = re.compile(r"[\r\n<>\"@,;]")
ADDRESS_RE = re.compile(r"^[^\s<>@,;\"]+@[^\s<>@,;\"]+\.[^\s<>@,;\"]+$")


# --------------------------------------------------------------------------- colors


def normalize_color(value: Any, default: str | None = None) -> str:
	"""'#ABC' / 'abc' / '#aabbcc' -> '#aabbcc'. ValueError (or `default`) when invalid."""
	text = str(value or "").strip()
	m = HEX_RE.match(text)
	if not m:
		if default is not None:
			return default
		raise ValueError("Color must be a hex value like #2f5bea")
	hexv = m.group(1).lower()
	if len(hexv) == 3:
		hexv = "".join(c * 2 for c in hexv)
	return "#" + hexv


def _rgb(color: str) -> tuple[int, int, int]:
	c = normalize_color(color)
	return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _hex(rgb: Iterable[float]) -> str:
	return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in rgb)


def mix(color: str, other: str, weight: float) -> str:
	"""`weight` of `other` mixed into `color` (0 = color, 1 = other)."""
	a, b = _rgb(color), _rgb(other)
	return _hex(x + (y - x) * weight for x, y in zip(a, b))


def relative_luminance(color: str) -> float:
	def chan(v: int) -> float:
		s = v / 255
		return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

	r, g, b = (chan(v) for v in _rgb(color))
	return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
	la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
	return (la + 0.05) / (lb + 0.05)


def on_color(color: str) -> str:
	"""Readable text color (#ffffff or #111111) on `color`."""
	return "#ffffff" if contrast_ratio(color, "#ffffff") >= contrast_ratio(color, "#111111") else "#111111"


def css_variables(primary: Any) -> dict[str, dict[str, str]]:
	"""Portal CSS variables for a primary color: `{"light": {...}, "dark": {...}}`."""
	p = normalize_color(primary, DEFAULT_PRIMARY)
	dark_p = mix(p, "#ffffff", 0.3)
	return {
		"light": {
			"--nx-primary": p,
			"--nx-primary-hover": mix(p, "#000000", 0.18),
			"--nx-primary-soft": mix(p, "#ffffff", 0.9),
			"--nx-on-primary": on_color(p),
		},
		"dark": {
			"--nx-primary": dark_p,
			"--nx-primary-hover": mix(dark_p, "#ffffff", 0.2),
			"--nx-primary-soft": mix(p, "#0d1117", 0.78),
			"--nx-on-primary": on_color(dark_p),
		},
	}


def css_block(primary: Any) -> str:
	"""<style> body for a brand (empty for the default color: dashboard.css already has it)."""
	p = normalize_color(primary, DEFAULT_PRIMARY)
	if p == DEFAULT_PRIMARY:
		return ""
	v = css_variables(p)

	def decl(d: Mapping[str, str]) -> str:
		return " ".join(f"{k}: {val};" for k, val in d.items())

	return (f":root {{ {decl(v['light'])} }}\n"
	        f"@media (prefers-color-scheme: dark) {{ :root {{ {decl(v['dark'])} }} }}")


# --------------------------------------------------------------------------- hosts / domains


def normalize_host(value: Any) -> str:
	"""Request host -> bare lowercase host name ('Portal.Example.com:443' -> 'portal.example.com')."""
	text = str(value or "").strip().lower()
	if "://" in text:
		text = text.split("://", 1)[1]
	text = text.split("/", 1)[0]
	if text.startswith("["):  # IPv6 literal
		return text.split("]", 1)[0] + "]"
	return text.rsplit(":", 1)[0].rstrip(".") if ":" in text else text.rstrip(".")


def parse_domains(value: Any) -> list[str]:
	"""Small Text (one per line, or comma/space separated) -> normalized unique host list.

	ValueError for an invalid host name."""
	if isinstance(value, (list, tuple)):
		items = [str(v) for v in value]
	else:
		items = re.split(r"[\s,]+", str(value or ""))
	out: list[str] = []
	for item in items:
		host = normalize_host(item)
		if not host:
			continue
		if not HOST_RE.match(host) or "." not in host:
			raise ValueError(f"Invalid portal domain: {item.strip()}")
		if host not in out:
			out.append(host)
	return out


def host_map(brands: Iterable[Mapping[str, Any]]) -> dict[str, str]:
	"""{host: brand name} for enabled brands (first brand wins on a duplicate host)."""
	out: dict[str, str] = {}
	for b in brands:
		if not b.get("enabled", 1):
			continue
		try:
			hosts = parse_domains(b.get("portal_domains"))
		except ValueError:
			continue
		for h in hosts:
			out.setdefault(h, b["name"])
	return out


def domain_conflicts(name: str, domains: Iterable[str], other_brands: Iterable[Mapping[str, Any]]) -> list[str]:
	"""Hosts of `domains` already claimed by another brand."""
	taken: dict[str, str] = {}
	for b in other_brands:
		if b.get("name") == name:
			continue
		try:
			for h in parse_domains(b.get("portal_domains")):
				taken[h] = b["name"]
		except ValueError:
			continue
	return [d for d in domains if d in taken]


def resolve_brand_name(host: Any, hosts: Mapping[str, str], default: str | None = None) -> str | None:
	"""Brand for a request host: exact match, then the 'www.' variant; else `default`."""
	h = normalize_host(host)
	if not h:
		return default
	if h in hosts:
		return hosts[h]
	alt = h[4:] if h.startswith("www.") else "www." + h
	return hosts.get(alt, default)


# --------------------------------------------------------------------------- public brand dict


def safe_url(value: Any) -> str | None:
	"""Site-relative path (/files/x.png) or https URL; anything else (javascript:, data:) is dropped."""
	text = str(value or "").strip()
	return text if text and SAFE_URL_RE.match(text) else None


def normalize_sender_name(value: Any) -> str:
	text = re.sub(r"\s+", " ", str(value or "")).strip()
	if SENDER_NAME_BAD.search(text):
		raise ValueError("Sender name cannot contain <, >, \", @, comma, semicolon or line breaks")
	if len(text) > 80:
		raise ValueError("Sender name is too long")
	return text


def public_brand(row: Mapping[str, Any] | None) -> dict[str, Any]:
	"""Brand fields for templates/boot (defaults filled, URLs and color sanitized)."""
	out = dict(DEFAULT_BRAND)
	if not row:
		out["css"] = ""
		return out
	out.update({k: row.get(k) for k in PUBLIC_FIELDS if row.get(k) not in (None, "")})
	out["is_default"] = bool(row.get("is_default")) if "is_default" in row else False
	out["name"] = row.get("name")
	out["title"] = str(row.get("title") or DEFAULT_BRAND["title"]).strip()[:80]
	out["logo"] = safe_url(row.get("logo"))
	out["favicon"] = safe_url(row.get("favicon"))
	out["primary_color"] = normalize_color(row.get("primary_color"), DEFAULT_PRIMARY)
	out["css"] = css_block(out["primary_color"])
	try:
		domains = parse_domains(row.get("portal_domains"))
	except ValueError:
		domains = []
	out["portal_domain"] = domains[0] if domains else None
	return out


def portal_url(brand: Mapping[str, Any] | None, path: str, default_base: str) -> str:
	"""Absolute URL of `path` on the brand's first portal domain, else on `default_base`."""
	domain = (brand or {}).get("portal_domain")
	base = f"https://{domain}" if domain else str(default_base or "").rstrip("/")
	return base + (path if path.startswith("/") else "/" + path)


# --------------------------------------------------------------------------- e-mail


def format_sender(name: Any, address: Any) -> str | None:
	"""'Acme Cloud <noreply@acme.test>' (None without an address)."""
	addr = str(address or "").strip()
	if not ADDRESS_RE.match(addr):
		return None
	try:
		label = normalize_sender_name(name)
	except ValueError:
		label = ""
	return f"{label} <{addr}>" if label else addr


def apply_footer(message: Any, footer: Any) -> str:
	"""Append the brand footer (escaped, line breaks kept) to an HTML/text message."""
	body = str(message or "")
	text = str(footer or "").strip()
	if not text:
		return body
	safe = html.escape(text).replace("\n", "<br>")
	return f'{body}<hr style="border:none;border-top:1px solid #e3e6eb;margin:24px 0 12px">' \
	       f'<div style="color:#5d6675;font-size:12px">{safe}</div>'


def brand_subject(subject: Any, brand_title: Any) -> str:
	"""Replace the '[Nexaploy]' prefix with the brand title."""
	text = str(subject or "")
	title = str(brand_title or "").strip()
	if title and title != "Nexaploy" and text.startswith("[Nexaploy]"):
		return f"[{title}]" + text[len("[Nexaploy]"):]
	return text

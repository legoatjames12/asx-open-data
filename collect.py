"""Collects open government data that bears on Australian shares into data/.

Runs on GitHub Actions every hour. Every source is public and needs no key or login, and each is stored with
the credit its publisher asks for: see data/README.md, which this script writes.
One source failing never stops the others. data/status.json says how each one went.
"""
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

DATA = "data"
SYDNEY = ZoneInfo("Australia/Sydney")
UTC = dt.timezone.utc
NAME = "asx-open-data-collector"
AGENT = f"{NAME} (+https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'local-test')})"
PAUSE = 0.4 # seconds between requests
BUDGET = 13 * 60 # seconds after which no further catch-up work is started
STARTED = time.monotonic()

# Announcement feeds, all seen working on 12 November 2026. Four others were tried and left out because their
# sites turn automated readers away: the industry and resources ministers, the trade minister, the
# infrastructure and communications ministers, and the health department.
FEEDS = {
"rba-media-releases": "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
"rba-speeches": "https://www.rba.gov.au/rss/rss-cb-speeches.xml",
"treasurer": "https://ministers.treasury.gov.au/ministers/jim-chalmers-2022/media-releases/feed",
"prime-minister": "https://www.pm.gov.au/rss.xml",
"accc": "https://www.accc.gov.au/rss/media_releases.xml",
"apra": "https://www.apra.gov.au/rss.xml",
"arena": "https://arena.gov.au/news/feed/",
"takeovers-panel": "https://takeovers.gov.au/rss.xml",
"us-federal-reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
}
REMEMBER = 300 # items no longer listed by a feed that are still remembered, so they are never recorded twice

# ASIC daily short positions: one file per trading day, kept exactly as published
SHORTS = "https://download.asic.gov.au/short-selling/"
SHORTS_FROM = "20211001"
SHORTS_PER_RUN = 150

# US Treasury daily par yield curve: one file per year
TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
"{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
TREASURY_FROM = 2021

# ABS Data API: monthly inflation and jobs. Read in the hours after an 11:30am Canberra release, and once more overnight.
ABS = "https://data.api.abs.gov.au/rest/data/"
ABS_SETS = {"consumer-price-index": "CPI/3...50.M", "labour-force": "LF/.3.1599..AUS.M"}
ABS_HOURS_UTC = (1, 2, 3, 14)

# AusTender contract notices: one file per Sydney day
TENDERS = "https://api.tenders.gov.au/ocds/findByDates/contractPublished/"
CONTRACTS_FROM = dt.date(2021, 11, 1)
CONTRACT_DAYS_PER_RUN = 60

README = """# Open data collected by collect.py

Everything in this folder comes from public government sources and is refreshed every hour by `collect.py`.
`status.json` says how each source went on the latest run. `state.json` is the collector's own memory.

| Folder | What it holds | Source and credit |
|---|---|---|
| `feeds/` | Headline, link and short extract of each new announcement, one file per Sydney day | The publishers' own RSS feeds. Each line names its source and links to the original. Copyright stays with each publisher. |
| `short-positions/` | Daily aggregate short position reports, unaltered | \u00a9 Australian Securities & Investments Commission: Reproduced with permission. Licensed CC BY 4.0. |
| `us-treasury/` | Daily par yield curve rates, one file per year, unaltered | U.S. Department of the Treasury. Public domain. |
| `abs/` | Monthly Consumer Price Index and Labour Force series, unaltered | Source: Australian Bureau of Statistics. Licensed CC BY 4.0. |
| `contracts/` | Commonwealth contract notices, one file per Sydney day of publication | Based on AusTender data, \u00a9 Commonwealth of Australia, licensed CC BY 3.0 AU. Fields have been selected and renamed; `sample-release.json` shows one notice as published. |

No source here endorses this collection or any use made of it.
"""

_robots = {}
_silent = {}
_sample_release = []
_empty_reply = []


def out_of_time():
return time.monotonic() - STARTED > BUDGET


def get(url, accept="*/*", limit=30_000_000):
"""GET one address. Returns (status, body, note). Status 0 means no answer came back at all."""
host = urllib.parse.urlsplit(url).netloc
if _silent.get(host, 0) >= 3:
return 0, b"", "host not answering on this run"
request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": accept})
try:
with urllib.request.urlopen(request, timeout=30) as response:
status, body = response.status, response.read(limit + 1)
if response.headers.get("Content-Encoding", "").lower() == "gzip":
body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(limit + 1)
note = ""
except urllib.error.HTTPError as error:
status, body, note = error.code, b"", ""
try:
body = error.read(2000) # the start of the error message, for the few callers that keep it
except Exception:
pass
except Exception as error: # no connection, a timeout, or a reply that cannot be unpacked
status, body, note = 0, b"", type(error).__name__
_silent[host] = _silent.get(host, 0) + 1
time.sleep(PAUSE)
if len(body) > limit:
return status, b"", f"reply larger than {limit // 1_000_000} MB"
return status, body, note


def robots_objection(url):
"""Why a general automated reader should not fetch this address, going by the site's robots.txt. Empty when it may.

Applied to feeds, which sit on ordinary websites. The data services below are published for programs to use.
"""
parts = urllib.parse.urlsplit(url)
site = f"{parts.scheme}://{parts.netloc}"
if site not in _robots:
status, body, note = get(site + "/robots.txt", limit=1_000_000)
if status == 200:
rules = urllib.robotparser.RobotFileParser()
rules.parse(body.decode("utf-8", "replace").splitlines())
_robots[site] = rules
elif 400 <= status < 500:
_robots[site] = "" # no robots.txt asks nothing of us
else:
_robots[site] = f"robots.txt could not be read (http {status} {note}".strip() + ")" # a site in trouble is left alone
rules = _robots[site]
if isinstance(rules, str):
return rules
return "" if rules.can_fetch(NAME, url) else "robots.txt does not allow it"


def put(path, data):
"""Write bytes to a file only if they differ from what is there. Returns True when the file changed."""
if os.path.exists(path):
with open(path, "rb") as handle:
if handle.read() == data:
return False
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "wb") as handle:
handle.write(data)
return True


def read_json(path, default):
try:
with open(path, encoding="utf-8") as handle:
return json.load(handle)
except (OSError, ValueError):
return default


def as_json(value):
return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def is_page(body):
"""True when a reply is a web page or an error message rather than a data file."""
head = body[:300].lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n")
return len(body) < 200 or head[:1] in (b"<", b"{")


def keep_sample(name, body):
"""Keep the start of an unexpected reply so the cause can be seen later."""
put(f"{DATA}/debug/{name}.bin", body[:20000])


def local(tag):
return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def tidy(text, limit):
"""Plain one-line text: markup removed, entities decoded, cut to a limit."""
text = re.sub(r"<[^>]*>", " ", text or "")
text = re.sub(r"<[^>]*>", " ", html.unescape(text))
text = re.sub(r"\s+", " ", text).strip()
return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


ZONES = {"AEDT": "+1100", "AEST": "+1000", "ACDT": "+1030", "ACST": "+0930", "AWST": "+0800"}


def stamp(text):
"""A feed's date as UTC text, or None when it cannot be read. A date that names no time zone is taken as Sydney time."""
text = re.sub(r"\b(%s)\b" % "|".join(ZONES), lambda match: ZONES[match.group(1)], (text or "").strip())
for parse in (parsedate_to_datetime, lambda value: dt.datetime.fromisoformat(value.replace("Z", "+00:00"))):
try:
when = parse(text)
except (TypeError, ValueError, IndexError):
continue
if when.tzinfo is None:
when = when.replace(tzinfo=UTC if text.endswith("-0000") else SYDNEY)
return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
for layout in ("%A %d %B %Y", "%A, %d %B %Y", "%d %B %Y", "%d %b %Y"): # a date in words, as the Prime Minister's feed gives it
try:
when = dt.datetime.strptime(text, layout).replace(tzinfo=SYDNEY)
except ValueError:
continue
return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
return None


def read_feed(body):
"""Items of an RSS or Atom feed, in the order the feed lists them (newest first)."""
root = ET.fromstring(body[max(body.find(b"<"), 0):])
items = []
for node in root.iter():
if local(node.tag) not in ("item", "entry"):
continue
found, link = {}, ""
for child in node:
key = local(child.tag)
if key == "link":
href = child.get("href")
if href is None:
link = link or "".join(child.itertext()).strip()
elif child.get("rel", "alternate") == "alternate":
link = link or href.strip()
elif key and key not in found:
value = "".join(child.itertext()).strip()
if value:
found[key] = value
link = link or next((value for name, value in node.attrib.items() if local(name) == "about"), "") or found.get("guid", "")
if not link.lower().startswith(("http://", "https://")):
link = ""
title = tidy(found.get("title"), 300)
if not (title or link):
continue
when = next((found[key] for key in ("pubDate", "date", "published", "updated", "issued") if key in found), "")
summary = next((found[key] for key in ("description", "summary", "encoded", "content") if key in found), "")
identity = found.get("guid") or found.get("id") or link or f"{title}|{when}"
items.append({
"key": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
"published": stamp(when),
"published_text": when[:60],
"title": title,
"link": link,
"summary": tidy(summary, 400),
})
if len(items) == 300:
break
return items


def collect_feeds(state, status, now):
memory = state.setdefault("feeds", {})
report = status["feeds"] = {}
lines = []
for name, url in FEEDS.items():
entry = report[name] = {}
if out_of_time():
entry["note"] = "not reached on this run"
continue
objection = robots_objection(url)
if objection:
entry["note"] = f"skipped: {objection}"
continue
code, body, note = get(url, "application/rss+xml, application/atom+xml, application/xml, text/xml, */*", 8_000_000)
if code != 200:
entry["note"] = f"http {code} {note}".strip()
continue
try:
items = read_feed(body)
except ET.ParseError:
items = []
if not items:
entry["note"] = "the reply held no feed items"
keep_sample(f"feed-{name}", body)
continue
known = memory.get(name)
backlog = known is None # first sight of this feed: everything it lists is old news, and is marked so
known = known or []
seen, listed = set(known), {item["key"] for item in items}
for item in reversed(items):
if item["key"] in seen:
continue
seen.add(item["key"])
line = {"seen": f"{now:%Y-%m-%dT%H:%M:%SZ}", "source": name, "published": item["published"],
"title": item["title"], "link": item["link"], "summary": item["summary"]}
if item["published_text"] and not item["published"]:
line["published_text"] = item["published_text"] # a date this script could not read, kept as written
if backlog:
line["backlog"] = True
lines.append(line)
memory[name] = [key for key in known if key not in listed][-REMEMBER:] + sorted(listed)
entry["listed"] = len(items)
entry["newest"] = max((item["published"] for item in items if item["published"]), default=None)
if lines:
day = f"{now.astimezone(SYDNEY):%Y-%m-%d}"
path = f"{DATA}/feeds/{day[:4]}/{day}.jsonl"
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "a", encoding="utf-8") as handle:
for line in lines:
handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def collect_shorts(state, status, now):
memory = state.setdefault("short_positions", {})
misses = memory.setdefault("misses", {})
if memory.get("complete") and now.hour % 3:
return # nothing outstanding: look for a new file eight times a day
report = status["short_positions"] = {}
folder = f"{DATA}/short-positions"
have = set()
for _, _, names in os.walk(folder):
for name in names:
match = re.fullmatch(r"RR(\d{8})-(\d{3})-SSDailyAggShortPos\.csv", name)
if match:
have.add(match.groups())
code, body, note = get(SHORTS + "short-selling-data.json", "application/json")
listed = []
if code == 200:
try:
rows = json.loads(body.decode("utf-8-sig"))
except ValueError:
rows = None
for row in rows if isinstance(rows, list) else []:
if isinstance(row, dict):
date, version = str(row.get("date")), str(row.get("version"))
if re.fullmatch(r"\d{8}", date) and re.fullmatch(r"\d{3}", version):
listed.append((date, version))
today = now.astimezone(SYDNEY).date()
recent = f"{today - dt.timedelta(days=14):%Y%m%d}"
if not listed:
# ASIC's list of files could not be read: try each recent weekday under the usual file name instead
report["note"] = f"list of files unavailable (http {code} {note}".strip() + "): trying recent weekdays"
days = [today - dt.timedelta(days=back) for back in range(1, 22)]
listed = [(f"{day:%Y%m%d}", "001") for day in days if day.weekday() < 5]
wanted = sorted({entry for entry in listed if entry[0] >= SHORTS_FROM}, reverse=True)
tried = 0
for date, version in wanted:
if (date, version) in have or misses.get(date, 0) >= 6:
continue
if tried == SHORTS_PER_RUN or out_of_time():
break
tried += 1
name = f"RR{date}-{version}-SSDailyAggShortPos.csv"
code, body, note = get(SHORTS + name)
if code == 200 and not is_page(body):
put(f"{folder}/{date[:4]}/{name}", body)
have.add((date, version))
misses.pop(date, None)
continue
if code == 200:
keep_sample("short-positions", body)
elif code not in (404, 410):
report["note"] = (report.get("note", "") + f" Stopped at {name}: http {code} {note}").strip()
break
if date < recent: # a new file that is not there yet is normal; an old one that is missing is tried six times
misses[date] = misses.get(date, 0) + 1
missing = [entry for entry in wanted if entry not in have]
memory["complete"] = not [date for date, _ in missing if date < recent and misses.get(date, 0) < 6]
stored = sorted(date for date, _ in have)
report.update(
stored=len(stored), oldest=stored[0] if stored else None, newest=stored[-1] if stored else None,
still_to_fetch=len([date for date, _ in missing if date < recent and misses.get(date, 0) < 6]),
waiting_on=sorted(date for date, _ in missing if date >= recent),
gave_up=sorted(date for date, count in misses.items() if count >= 6)[-40:],
)


def collect_treasury(state, status, now):
report = status["us_treasury"] = {}
for year in range(TREASURY_FROM, now.year + 1):
path = f"{DATA}/us-treasury/par-yield-curve-{year}.csv"
closed = year < now.year and not (year == now.year - 1 and now.month == 1 and now.day <= 10)
if closed and os.path.exists(path):
continue
code, body, note = get(TREASURY.format(year=year), "text/csv, */*")
if code == 200 and not is_page(body):
put(path, body)
else:
report[str(year)] = f"http {code} {note}".strip()
if code == 200:
keep_sample("us-treasury", body)
try:
with open(f"{DATA}/us-treasury/par-yield-curve-{now.year}.csv", encoding="utf-8", errors="replace") as handle:
handle.readline()
report["newest_row"] = handle.readline().split(",")[0].strip('" \r\n') or None
except OSError:
report["newest_row"] = None
report["files"] = len(os.listdir(f"{DATA}/us-treasury")) if os.path.isdir(f"{DATA}/us-treasury") else 0


def collect_abs(state, status, now):
paths = {name: f"{DATA}/abs/{name}.csv" for name in ABS_SETS}
if state.get("abs_tried") and now.hour not in ABS_HOURS_UTC:
return
state["abs_tried"] = True
report = status["abs"] = {}
for name, key in ABS_SETS.items():
forms = [
(f"{ABS}{key}?startPeriod=2019-01", "application/vnd.sdmx.data+csv;labels=both"),
(f"{ABS}{key}?startPeriod=2019-01&format=csv", "*/*"),
(f"{ABS}ABS,{key}?startPeriod=2019-01&format=csv", "*/*"),
]
replies = []
for number, (url, accept) in enumerate(forms, 1):
code, body, note = get(url, accept)
if code == 200 and not is_page(body):
put(paths[name], body)
report[name] = {"request_form": number, "lines": body.count(b"\n")}
break
replies.append(f"{code} {note}".strip())
if code == 200:
keep_sample(f"abs-{name}", body)
else:
report[name] = {"note": "no form of the request returned data: http " + ", ".join(replies)}


def slim(release):
"""The useful fields of one AusTender notice."""
parties = [party for party in release.get("parties") or [] if isinstance(party, dict)]
supplier = next((party for party in parties if "supplier" in (party.get("roles") or [])), {})
agency = next((party for party in parties if "procuringEntity" in (party.get("roles") or [])), {})
contract = next((entry for entry in release.get("contracts") or [] if isinstance(entry, dict)), {})
value, period = contract.get("value") or {}, contract.get("period") or {}
category = ""
for holder in (contract, release.get("tender") or {}):
for item in holder.get("items") or []:
sort = (item or {}).get("classification") or {}
category = category or " ".join(str(sort[key]) for key in ("id", "description") if sort.get(key))
tag = release.get("tag")
try:
amount = float(value.get("amount"))
except (TypeError, ValueError):
amount = None
return {
"cn": contract.get("id"),
"published": release.get("date"),
"tag": " ".join(tag) if isinstance(tag, list) else tag,
"agency": agency.get("name"),
"supplier": supplier.get("name"),
"abn": next((entry.get("id") for entry in supplier.get("additionalIdentifiers") or []
if isinstance(entry, dict) and entry.get("scheme") == "AU-ABN"), None),
"amount": amount,
"start": period.get("startDate"),
"end": period.get("endDate"),
"description": tidy(contract.get("description") or "", 600),
"category": category or None,
}


def contracts_on(day):
"""Contract notices published on one Sydney day. Returns (rows, note): rows is None when the day could not be read."""
start = dt.datetime.combine(day, dt.time(0), SYDNEY).astimezone(UTC)
end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), SYDNEY).astimezone(UTC)
url = f"{TENDERS}{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
rows, asked = [], set()
while len(asked) < 80:
asked.add(url)
code, body, note = get(url, "application/json")
if code in (400, 404) and len(asked) == 1:
_empty_reply.append(body) # AusTender answers a window with no notices with "400": one such reply is kept to look at
return [], ""
if code != 200:
return None, f"http {code} {note}".strip()
try:
page = json.loads(body)
except ValueError:
return None, "http 200 but the reply was not JSON"
releases = page.get("releases") if isinstance(page, dict) else None
if not isinstance(releases, list):
if len(asked) == 1:
return [], ""
return None, "http 200 but a later page held no notices"
releases = [release for release in releases if isinstance(release, dict)]
if releases and not _sample_release:
_sample_release.append(releases[0])
rows += [slim(release) for release in releases]
following = page["links"].get("next") if isinstance(page.get("links"), dict) else None
if not following:
rows.sort(key=lambda row: (str(row["published"]), str(row["cn"])))
return rows, ""
url = urllib.parse.urljoin(url, following) if isinstance(following, str) else ""
if not url.startswith("https://api.tenders.gov.au/") or url in asked:
return None, "the address of the next page was not understood"
return None, "more than 80 pages in one day"


def collect_contracts(state, status, now):
memory = state.setdefault("contracts", {})
report = status["contracts"] = {}
yesterday = now.astimezone(SYDNEY).date() - dt.timedelta(days=1)
through = dt.date.fromisoformat(memory["through"]) if memory.get("through") else yesterday - dt.timedelta(days=3)
back_to = dt.date.fromisoformat(memory["back_to"]) if memory.get("back_to") else through + dt.timedelta(days=1)
stalls = memory.setdefault("stalls", {})
notes = []

def take(day):
"""Fetch and store one day. Returns the number of notices, or None when the day should be tried again next run."""
rows, note = contracts_on(day)
key = day.isoformat()
if rows is None:
notes.append(f"{key}: {note}")
if not note.startswith("http 200"):
return None # the service is down, busy or turning us away: wait for it
stalls[key] = stalls.get(key, 0) + 1
if stalls[key] < 6:
return None
memory["skipped"] = sorted(set(memory.get("skipped", []) + [key])) # unreadable on six runs: move on, keep a record
rows = []
stalls.pop(key, None)
if rows:
text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
put(f"{DATA}/contracts/{day.year}/{key}.jsonl", text.encode("utf-8"))
return len(rows)

def walk(day, step, last, limit, empty):
"""Take days one at a time towards `last`. Returns the furthest day settled.

`empty` lists the weekdays in a row that came back with nothing. Ten of them means the service has
changed rather than gone quiet, so the walk goes back to the first and holds there until notices return.
"""
settled, done = day - step, 0
while (day <= last if step.days > 0 else day >= last) and done < limit and not out_of_time():
count = take(day)
if count is None:
break
if count:
empty.clear()
elif day.weekday() < 5:
empty.append(day.isoformat())
if len(empty) >= 10:
notes.append(f"ten weekdays in a row with no notices, from {empty[0]} to {day}: holding there")
first = dt.date.fromisoformat(empty[0])
empty.clear()
return first - step
settled, day, done = day, day + step, done + 1
return settled

one = dt.timedelta(days=1)
through = walk(through + one, one, yesterday, 400, memory.setdefault("empty_weekdays", []))
back_to = walk(back_to - one, -one, CONTRACTS_FROM, CONTRACT_DAYS_PER_RUN, [])
memory.update(through=through.isoformat(), back_to=back_to.isoformat())
if _sample_release and not os.path.exists(f"{DATA}/contracts/sample-release.json"):
put(f"{DATA}/contracts/sample-release.json", as_json(_sample_release[0]))
if _empty_reply and not os.path.exists(f"{DATA}/debug/contracts-empty-reply.bin"):
keep_sample("contracts-empty-reply", _empty_reply[0] or b"(no text in the reply)")
files = [name for _, _, names in os.walk(f"{DATA}/contracts") for name in names if name.endswith(".jsonl")]
report.update(through=memory["through"], back_to=memory["back_to"], target_start=CONTRACTS_FROM.isoformat(),
days_with_notices=len(files), newest_file=max(files, default=None),
skipped=memory.get("skipped", [])[-40:], notes=notes[:5])


JOBS = (
("feeds", collect_feeds),
("short_positions", collect_shorts),
("us_treasury", collect_treasury),
("abs", collect_abs),
("contracts", collect_contracts),
)


def main(now=None):
now = now or dt.datetime.now(UTC)
os.makedirs(DATA, exist_ok=True)
state = read_json(f"{DATA}/state.json", {})
previous = read_json(f"{DATA}/status.json", {})
previous.pop("written_utc", None)
status = json.loads(json.dumps(previous))
for name, job in JOBS:
try:
job(state, status, now)
except Exception: # a fault in one collector is recorded and the rest carry on
status.setdefault(name, {})["crash"] = traceback.format_exc(limit=4)[-700:]
put(f"{DATA}/state.json", as_json(state))
put(f"{DATA}/README.md", README.encode("utf-8"))
put(f"{DATA}/.gitattributes", b"* -text\n")
status["day"] = f"{now:%Y-%m-%d}"
if status != previous: # written when something changed, and once a day as a sign of life
status["written_utc"] = f"{now:%Y-%m-%dT%H:%MZ}"
put(f"{DATA}/status.json", as_json(status))
print(json.dumps(status, indent=1, sort_keys=True, ensure_ascii=False))
return 0


if __name__ == "__main__":
sys.exit(main())   

"""Weekly review roundup for a list of Marriott and IHG hotels (Bazaarvoice).
Designed to run on a GitHub Actions cron every Friday.

For each hotel in hotels.json it pulls the current review statistics and the
recent reviews from the public Bazaarvoice API that marriott.com and ihg.com
use for their own review pages, compares them to last week's snapshot in
state.json, and writes roundup.md. The workflow opens a GitHub Issue from it,
and GitHub emails the repo owner the issue content.

If ANTHROPIC_API_KEY is set, the new reviews for each hotel are summarized by
Claude. Without it, a plain statistical summary is used instead.

The first run for a hotel sets its baseline: it is included in the roundup
with its current numbers and zero changes.

Test mode: set TEST_FAKE_REVIEWS=N to add one made-up review to N random
hotels on top of the live data. The roundup is titled [TEST], hotels without
a baseline are compared against their current numbers, and state.json is not
changed.
"""

import json
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "hotels.json"
STATE_FILE = ROOT / "state.json"
ROUNDUP_FILE = ROOT / "roundup.md"
TITLE_FILE = ROOT / "roundup_title.txt"

API_BASE = "https://api.bazaarvoice.com/data/reviews.json"
PAGE_SIZE = 100          # Bazaarvoice maximum
MAX_PAGES = 10           # safety cap per hotel per run
LOOKBACK_DAYS = 60       # how far back to look for reviews that were published late
EXCERPT_CHARS = 400
ISSUE_BODY_LIMIT = 60000  # GitHub caps issue bodies at 65,536 characters
STARS = [5, 4, 3, 2, 1]

FAKE_REVIEWS = [
    (5, "Fantastic stay", "Spotless room, very friendly front desk team and a great breakfast. "
        "The pool area was relaxing and the staff remembered our names."),
    (4, "Lovely hotel, small niggles", "Great location and comfortable beds. Check-in took a "
        "while and the room service menu was limited, but we would come back."),
    (2, "Disappointing for the price", "Room smelled of damp, the air conditioning was noisy "
        "all night and housekeeping skipped our room twice. Staff apologised but nothing changed."),
    (1, "Would not return", "Waited 45 minutes to check in, the room was not cleaned properly "
        "and our complaint to reception was ignored."),
]

CLAUDE_MODEL = "claude-opus-5-5"
SUMMARY_PROMPT = """You are writing one section of a weekly guest review roundup for a hotel \
asset manager. Below are the guest reviews posted this week for {hotel}.

Write 3 to 5 short bullet points covering:
- what guests praised
- what guests complained about, naming specific operational issues (cleanliness, staff, \
noise, check-in, breakfast, maintenance, etc.)
- anything in a low-score review (1 or 2 stars) that management should follow up on

Be specific and factual, quote short phrases where useful, and do not invent details. \
Plain markdown bullets only, no heading, no preamble. Do not use em dashes.

Reviews:
{reviews}"""


# ---------------------------------------------------------------- data access

def api_get(params):
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "review-roundup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("HasErrors"):
        raise RuntimeError(f"Bazaarvoice API error: {data.get('Errors')}")
    return data


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def product_stats(data, product_id):
    products = data.get("Includes", {}).get("Products", {})
    for key, product in products.items():
        if key.lower() == product_id.lower():
            return product.get("ReviewStatistics", {})
    return {}


def fetch_hotel(hotel, passkey, since):
    """Return (stats, reviews submitted on or after `since`), newest first."""
    stats = None
    reviews = []
    for page in range(MAX_PAGES):
        params = {
            "apiversion": "5.5",
            "passkey": passkey,
            "Filter": f"ProductId:{hotel['product_id'].upper()}",
            "Sort": "SubmissionTime:desc",
            "Limit": PAGE_SIZE,
            "Offset": page * PAGE_SIZE,
        }
        if page == 0:
            params["Include"] = "Products"
            params["Stats"] = "Reviews"
        data = api_get(params)
        if stats is None:
            stats = product_stats(data, hotel["product_id"])
        results = data.get("Results", [])
        for r in results:
            if parse_time(r["SubmissionTime"]) < since:
                return stats, reviews
            reviews.append(r)
        if len(results) < PAGE_SIZE:
            break
    return stats or {}, reviews


def snapshot(stats):
    dist = {str(s): 0 for s in STARS}
    for row in stats.get("RatingDistribution") or []:
        dist[str(row["RatingValue"])] = row["Count"]
    return {
        "total": stats.get("TotalReviewCount"),
        "average": stats.get("AverageOverallRating"),
        "distribution": dist,
    }


# ---------------------------------------------------------------- summaries

def review_line(r):
    text = " ".join((r.get("ReviewText") or "").split())
    return (
        f"[{r.get('Rating')}/5] {r.get('SubmissionTime', '')[:10]} "
        f"\"{r.get('Title') or ''}\": {text}"
    )


def claude_summary(client, hotel_name, reviews):
    import anthropic

    prompt = SUMMARY_PROMPT.format(
        hotel=hotel_name, reviews="\n\n".join(review_line(r) for r in reviews)
    )
    try:
        response = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        print(f"WARNING: Claude API error for {hotel_name}: {exc.status_code} {exc.message}",
              file=sys.stderr)
        return None
    except anthropic.APIConnectionError as exc:
        print(f"WARNING: could not reach Claude API for {hotel_name}: {exc}", file=sys.stderr)
        return None
    if response.stop_reason == "refusal":
        print(f"WARNING: summary declined for {hotel_name}", file=sys.stderr)
        return None
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def add_fake_review(h, now):
    """Test mode: put one made-up review on a hotel's results and stats."""
    rating, title, text = random.choice(FAKE_REVIEWS)
    review = {
        "Id": f"test-{random.randint(100000, 999999)}",
        "SubmissionTime": now.isoformat(timespec="seconds"),
        "Rating": rating,
        "Title": f"[TEST] {title}",
        "ReviewText": text,
        "UserNickname": "test-guest",
    }
    cur = {**h["cur"], "distribution": dict(h["cur"]["distribution"])}
    if cur["total"] is not None and cur["average"] is not None:
        cur["average"] = (cur["average"] * cur["total"] + rating) / (cur["total"] + 1)
        cur["total"] += 1
    cur["distribution"][str(rating)] = cur["distribution"].get(str(rating), 0) + 1
    h["cur"] = cur
    h["new"] = [review] + h["new"]


def basic_summary(reviews):
    ratings = [r["Rating"] for r in reviews if isinstance(r.get("Rating"), int)]
    if not ratings:
        return "_No ratings on the new reviews._"
    avg = sum(ratings) / len(ratings)
    low = [r for r in reviews if isinstance(r.get("Rating"), int) and r["Rating"] <= 2]
    lines = [f"- Average rating of this week's reviews: **{avg:.2f}/5** across {len(ratings)}"]
    if low:
        titles = "; ".join(f"\"{r.get('Title') or 'untitled'}\" ({r['Rating']}/5)" for r in low)
        lines.append(f"- **{len(low)} low-score review(s):** {titles}")
    return "\n".join(lines)


# ---------------------------------------------------------------- formatting

def fmt_delta(value, digits=0):
    if value is None:
        return "n/a"
    if digits:
        return f"{value:+.{digits}f}"
    return f"{value:+,d}"


def fmt_num(value, digits=0):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" if digits else f"{value:,d}"


def star_counts(reviews):
    counts = {s: 0 for s in STARS}
    for r in reviews:
        if r.get("Rating") in counts:
            counts[r["Rating"]] += 1
    return counts


def text_table(headers, rows):
    """Fixed-width table in a code block. Email clients render code blocks in a
    monospace font, so columns stay aligned where markdown tables lose them."""
    cols = list(zip(headers, *rows))
    widths = [max(len(str(v)) for v in col) for col in cols]

    def line(values):
        cells = [str(v).ljust(w) if i == 0 else str(v).rjust(w)
                 for i, (v, w) in enumerate(zip(values, widths))]
        return " | ".join(cells).rstrip()

    rule = "-+-".join("-" * w for w in widths)
    return "\n".join(["```text", line(headers), rule] + [line(r) for r in rows] + ["```"])


def format_review(r):
    rating = r.get("Rating")
    low = " **LOW SCORE**" if isinstance(rating, int) and rating <= 2 else ""
    text = " ".join((r.get("ReviewText") or "").split())
    if len(text) > EXCERPT_CHARS:
        text = text[:EXCERPT_CHARS] + "..."
    return (
        f"- **{rating}/5**{low} | {r.get('SubmissionTime', '')[:10]} | "
        f"**{r.get('Title') or 'untitled'}** ({r.get('UserNickname') or 'anonymous'})\n"
        f"  > {text}"
    )


def hotel_section(h, include_reviews=True):
    prev, cur, new = h["prev"], h["cur"], h["new"]
    counts = star_counts(new)
    out = [f"### {h['name']} ({h['brand'].upper()})", ""]

    total_d = (cur["total"] - prev["total"]) if None not in (cur["total"], prev["total"]) else None
    avg_d = (cur["average"] - prev["average"]) if None not in (cur["average"], prev["average"]) else None
    out.append(f"- **Total reviews:** {fmt_num(cur['total'])} ({fmt_delta(total_d)} vs last week)")
    out.append(f"- **Average rating:** {fmt_num(cur['average'], 2)}/5 ({fmt_delta(avg_d, 2)} vs last week)")
    out.append(f"- **New reviews this week:** {len(new)}")
    out.append("")

    rows = []
    for s in STARS:
        now_c = cur["distribution"].get(str(s), 0)
        was_c = prev["distribution"].get(str(s), 0)
        rows.append([f"{s} star", counts[s], f"{now_c:,d}", fmt_delta(now_c - was_c)])
    out.append(text_table(["Rating", "New this week", "All-time", "Change"], rows))
    out.append("")

    if new:
        out.append("**Summary of new reviews**")
        out.append("")
        out.append(h["summary"])
        out.append("")
        if include_reviews:
            out.append(f"<details><summary>All {len(new)} new review(s)</summary>")
            out.append("")
            out.extend(format_review(r) for r in new)
            out.append("")
            out.append("</details>")
            out.append("")
    if h.get("reviews_url"):
        out.append(f"[View all reviews]({h['reviews_url']})")
        out.append("")
    return "\n".join(out)


def build_report(results, baselined, skipped, errors, period_start, period_end):
    def body(include_reviews):
        lines = [
            f"Guest review roundup for **{period_start:%b %d} to {period_end:%b %d, %Y}** "
            "(marriott.com and ihg.com, via Bazaarvoice).",
            "",
        ]
        if results:
            totals = [h["cur"]["total"] for h in results if h["cur"]["total"] is not None]
            weighted = sum(
                h["cur"]["total"] * h["cur"]["average"] for h in results
                if h["cur"]["total"] and h["cur"]["average"] is not None
            )
            total_new = sum(len(h["new"]) for h in results)
            lines += ["## Portfolio overview", ""]
            if totals and sum(totals):
                lines += [
                    f"**{len(results)} hotels, {sum(totals):,d} reviews in total, "
                    f"weighted average rating {weighted / sum(totals):.2f}/5, "
                    f"{total_new} new review(s) this week.**",
                    "",
                ]
            rows = []
            for h in results:
                prev, cur = h["prev"], h["cur"]
                total_d = (cur["total"] - prev["total"]) if None not in (cur["total"], prev["total"]) else None
                avg_d = (cur["average"] - prev["average"]) if None not in (cur["average"], prev["average"]) else None
                c = star_counts(h["new"])
                rows.append(
                    [h["short_name"], fmt_num(cur["total"]), fmt_delta(total_d),
                     fmt_num(cur["average"], 2), fmt_delta(avg_d, 2), len(h["new"])]
                    + [c[s] for s in STARS]
                )
            lines.append("Chg = change vs last week. 5* to 1* = new reviews this week by star rating.")
            lines.append("")
            lines.append(text_table(
                ["Hotel", "Reviews", "Chg", "Rating", "Chg", "New", "5*", "4*", "3*", "2*", "1*"], rows
            ))
            lines += ["", "## Hotel detail", ""]
            lines += [hotel_section(h, include_reviews) for h in results]
        if not include_reviews:
            lines.append("_Full review listings were left out to fit GitHub's issue size limit._\n")
        if baselined:
            lines.append("**First run for these hotels, so changes show as 0 and are reported from next week:** "
                         + ", ".join(baselined) + "\n")
        if skipped:
            lines.append("**Skipped:**\n" + "\n".join(f"- {s}" for s in skipped) + "\n")
        if errors:
            lines.append("**Errors:**\n" + "\n".join(f"- {e}" for e in errors) + "\n")
        return "\n".join(lines)

    text = body(include_reviews=True)
    if len(text) > ISSUE_BODY_LIMIT:
        text = body(include_reviews=False)
    return text[:ISSUE_BODY_LIMIT]


# ---------------------------------------------------------------- main

def main():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    passkeys = config.get("passkeys", {})
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    hotel_state = state.get("hotels", {})

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    last_run = parse_time(state["lastRun"]) if state.get("lastRun") else now - timedelta(days=7)

    fake_count = int(os.environ.get("TEST_FAKE_REVIEWS") or 0)

    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()

    results, baselined, skipped, errors = [], [], [], []
    new_state = {}

    for hotel in config["hotels"]:
        if hotel.get("enabled") is False:
            continue
        key = f"{hotel['brand'].lower()}:{hotel['product_id'].lower()}"
        name = hotel["name"]
        passkey = (
            hotel.get("passkey")
            or os.environ.get(f"BV_PASSKEY_{hotel['brand'].upper()}")
            or passkeys.get(hotel["brand"].lower())
        )
        if not passkey:
            skipped.append(f"{name}: no Bazaarvoice passkey set for brand '{hotel['brand']}'")
            if key in hotel_state:
                new_state[key] = hotel_state[key]
            continue

        try:
            stats, reviews = fetch_hotel(hotel, passkey, since)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            if key in hotel_state:
                new_state[key] = hotel_state[key]
            continue

        cur = snapshot(stats)
        seen_window = {r["Id"]: r["SubmissionTime"] for r in reviews}
        prev_state = hotel_state.get(key)

        if prev_state is None:
            baselined.append(name)
            prev_state = {**cur, "seenIds": seen_window}

        seen = prev_state.get("seenIds", {})
        new = [r for r in reviews if r["Id"] not in seen]
        # Keep IDs still inside the lookback window so late-published reviews
        # are not reported twice.
        kept = {i: t for i, t in seen.items() if parse_time(t) >= since}
        kept.update(seen_window)
        new_state[key] = {**cur, "seenIds": kept}

        results.append({
            "name": name,
            "short_name": hotel.get("short_name") or name,
            "brand": hotel["brand"],
            "reviews_url": hotel.get("reviews_url"),
            "prev": {k: prev_state.get(k) for k in ("total", "average", "distribution")},
            "cur": cur,
            "new": new,
        })

    if fake_count:
        for h in random.sample(results, min(fake_count, len(results))):
            add_fake_review(h, now)
    else:
        STATE_FILE.write_text(
            json.dumps({"lastRun": now.isoformat(timespec="seconds"), "hotels": new_state}, indent=2),
            encoding="utf-8",
        )

    for h in results:
        if h["new"]:
            summary = claude_summary(client, h["name"], h["new"]) if client else None
            h["summary"] = summary or basic_summary(h["new"])

    for msg in skipped + errors:
        print(f"WARNING: {msg}", file=sys.stderr)

    if not results and not errors and not skipped:
        print("No hotels to report on. No roundup created.")
        return
    if errors and not results and not baselined:
        raise RuntimeError("every hotel failed: " + "; ".join(errors))

    total_new = sum(len(h["new"]) for h in results)
    low = sum(1 for h in results for r in h["new"] if isinstance(r.get("Rating"), int) and r["Rating"] <= 2)
    flag = f" [{low} LOW SCORE]" if low else ""
    if results:
        title = f"Weekly Review Roundup: {total_new} new review(s) across {len(results)} hotel(s){flag} (week ending {now:%Y-%m-%d})"
    else:
        title = f"Weekly Review Roundup: setup notes (week ending {now:%Y-%m-%d})"
    if fake_count:
        title = "[TEST] " + title
    TITLE_FILE.write_text(title, encoding="utf-8")
    ROUNDUP_FILE.write_text(
        build_report(results, baselined, skipped, errors, last_run, now), encoding="utf-8"
    )
    print(f"Roundup written: {total_new} new review(s) across {len(results)} hotel(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

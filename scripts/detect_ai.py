#!/usr/bin/env python3
"""Run voice samples through AI-speech detectors and print one verdict per file.

Providers are enabled by the API keys present in the environment / .env:
  MODULATE_API_KEY, RESEMBLE_API_KEY, REALITY_DEFENDER_API_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import requests

MODULATE_URL = "https://platform.modulate.ai/api/velma-2-synthetic-voice-detection-batch"
RESEMBLE_URL = "https://app.resemble.ai/api/v2/detect"

# A clip whose average is "real" but which contains a frame this confidently
# synthetic is probably spliced, so it goes to review instead of passing.
PARTIAL_PEAK = 0.8

# Connect directly unless --proxy is given: an ambient proxy (macOS system
# setting or HTTPS_PROXY) that cannot reach a detector would fail every call.
http = requests.Session()
http.trust_env = False


def load_env() -> None:
    for env_file in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def result(label: str, score: float | None = None, detail: str = "", fields: dict | None = None) -> dict:
    """label is one of: fake, real, partial, unknown. score is P(fake) when the provider defines one.

    fields holds the provider's own metrics by display name, for callers that format their own report.
    """
    return {"label": label, "score": score, "detail": detail, "fields": fields or {}}


def check(r: requests.Response) -> None:
    """Raise with the provider's own error message, which says more than the status code."""
    if r.ok:
        return
    try:
        body = r.json()
        message = body.get("message") or body.get("error") or body.get("detail") or r.text
    except ValueError:
        message = r.text
    raise RuntimeError(f"HTTP {r.status_code}: {str(message)[:200]}")


def modulate(path: Path, args: argparse.Namespace) -> dict:
    # Cheap and idempotent, so transient failures are worth retrying.
    for attempt in range(3):
        try:
            with path.open("rb") as f:
                r = http.post(
                    MODULATE_URL,
                    headers={"X-API-Key": os.environ["MODULATE_API_KEY"]},
                    files={"upload_file": (path.name, f)},
                    timeout=300,
                )
        except requests.RequestException:
            if attempt == 2:
                raise
        else:
            if attempt == 2 or not (r.status_code == 429 or r.status_code >= 500):
                break
        time.sleep(2 * (attempt + 1))
    check(r)
    frames = [f for f in r.json()["frames"] if f["verdict"] != "no-content"]
    if not frames:
        return result("unknown", detail="no speech found")

    weighted, total, peak = 0.0, 0, 0.0
    for f in frames:
        p_fake = f["confidence"] if f["verdict"] == "synthetic" else 1 - f["confidence"]
        duration = f["end_time_ms"] - f["start_time_ms"]
        weighted += p_fake * duration
        total += duration
        peak = max(peak, p_fake)
    score = weighted / total
    detail = f"{len(frames)} frames, peak {peak:.2f}"
    fields = {"AI_probability": round(score, 4), "Peak_frame": round(peak, 4)}
    if score >= 0.5:
        return result("fake", score, detail, fields)
    if peak >= PARTIAL_PEAK:
        return result("partial", score, detail, fields)
    return result("real", score, detail, fields)


def resemble(path: Path, args: argparse.Namespace) -> dict:
    headers = {"Authorization": f"Bearer {os.environ['RESEMBLE_API_KEY']}"}
    data = {"zero_retention_mode": "true"} if args.zero_retention else {}

    def submit(extra: dict) -> requests.Response:
        with path.open("rb") as f:
            return http.post(
                RESEMBLE_URL, headers=headers, files={"file": (path.name, f)}, data={**data, **extra}, timeout=300
            )

    # Resemble bills per second analysed, so only send the opening of the clip.
    max_seconds = float(os.environ.get("RESEMBLE_MAX_SECONDS", "20"))
    if max_seconds > 0:
        r = submit({"start_region": "0", "end_region": str(max_seconds)})
        if r.status_code == 422 and "outside the media duration" in r.text:
            r = submit({})  # the clip is shorter than the cap
    else:
        r = submit({})
    check(r)
    item = r.json()["item"]

    deadline = time.time() + args.timeout
    while item["status"] == "processing":
        if time.time() > deadline:
            return result("unknown", detail=f"still processing after {args.timeout}s (uuid {item['uuid']})")
        time.sleep(3)
        r = http.get(f"{RESEMBLE_URL}/{item['uuid']}", headers=headers, timeout=60)
        check(r)
        item = r.json()["item"]
    if item["status"] != "completed":
        return result("unknown", detail=f"status {item['status']}")

    metrics = item["metrics"]
    # Resemble documents the label but not the direction of aggregated_score,
    # so the verdict uses the label and the score is reported as-is.
    detail = f"aggregated_score {metrics.get('aggregated_score')}, consistency {metrics.get('consistency')}"
    label = metrics["label"] if metrics["label"] in ("fake", "real") else "unknown"
    fields = {"Aggregated_score": metrics.get("aggregated_score"), "Consistency": metrics.get("consistency")}
    return result(label, detail=detail, fields=fields)


def reality_defender(path: Path, args: argparse.Namespace) -> dict:
    try:
        from realitydefender import RealityDefender
    except ImportError:
        return result("unknown", detail="pip install realitydefender to enable")

    async def run() -> dict:
        rd = RealityDefender(api_key=os.environ["REALITY_DEFENDER_API_KEY"])
        upload = await rd.upload(file_path=str(path))
        return await rd.get_result(upload["request_id"])

    res = asyncio.run(run())
    status = str(res.get("status", "")).upper()
    label = {"MANIPULATED": "fake", "FAKE": "fake", "AUTHENTIC": "real"}.get(status, "unknown")
    return result(label, res.get("score"), f"status {status}", {"Score": res.get("score")})


PROVIDERS = {
    "modulate": ("MODULATE_API_KEY", modulate),
    "resemble": ("RESEMBLE_API_KEY", resemble),
    "realitydefender": ("REALITY_DEFENDER_API_KEY", reality_defender),
}


def overall(results: dict[str, dict]) -> str:
    labels = [r["label"] for r in results.values()]
    if "fake" in labels:
        # Detectors that disagree are a reason to listen, not a verdict.
        return "REVIEW" if "real" in labels else "FLAG"
    # A detector that errored does not block a verdict another one reached.
    if "partial" in labels or "real" not in labels:
        return "REVIEW"
    return "PASS" if labels.count("real") > 1 else "PASS (single detector)"


def configured(wanted: list[str] | None = None) -> list[str]:
    """Providers that have an API key set, in the order given."""
    return [name for name in (wanted or PROVIDERS) if os.environ.get(PROVIDERS[name][0])]


def detect(path: Path, active: list[str], args: argparse.Namespace, escalate: bool = False) -> dict:
    """Run the detectors on one file.

    With escalate, the first detector decides alone when it says "real"; the
    others (which may cost far more per call) run only as a second opinion.
    """
    results = {}
    for name in active:
        try:
            results[name] = PROVIDERS[name][1](path, args)
        except Exception as e:  # one provider failing must not hide the others
            results[name] = result("unknown", detail=f"error: {e}")
        if escalate and results[name]["label"] == "real":
            break
    return {"file": str(path), "verdict": overall(results), "providers": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--providers", help="comma-separated subset of: " + ",".join(PROVIDERS))
    parser.add_argument("--zero-retention", action="store_true", help="ask providers that support it to delete the audio after analysis")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for async providers")
    parser.add_argument("--proxy", help="proxy URL to use for detector requests (default: connect directly)")
    parser.add_argument("--escalate", action="store_true", help="run later providers only when the first one does not say real")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    load_env()
    if args.proxy:
        http.proxies = {"http": args.proxy, "https": args.proxy}
    wanted = args.providers.split(",") if args.providers else list(PROVIDERS)
    unknown = [name for name in wanted if name not in PROVIDERS]
    if unknown:
        parser.error(f"unknown provider(s): {', '.join(unknown)}")
    active = configured(wanted)
    if not active:
        keys = ", ".join(PROVIDERS[name][0] for name in wanted)
        print(f"No detector is configured. Set at least one of: {keys}", file=sys.stderr)
        return 2

    report = []
    for path in args.files:
        if not path.is_file():
            print(f"{path}: not a file", file=sys.stderr)
            return 2
        report.append(detect(path, active, args, args.escalate))

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    for entry in report:
        print(f"{entry['verdict']:<8} {entry['file']}")
        for name, r in entry["providers"].items():
            score = "" if r["score"] is None else f" p_fake={r['score']:.2f}"
            print(f"    {name:<16} {r['label']:<8}{score}  {r['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

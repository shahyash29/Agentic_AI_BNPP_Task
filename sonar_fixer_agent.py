"""SonarQube -> Gemini fixer with risk assessment and mandatory human approval.

Nothing is written to disk unless you type 'y' for that specific change.

Setup:
    pip install requests
    export SONAR_TOKEN=...   GEMINI_API_KEY=...
    python sonar_gemini_fixer.py        # run from the project root
"""
import ast
import difflib
import json
import os
import shutil
import sys
import requests

SONAR_URL = os.getenv("SONAR_URL", "http://localhost:9000")
SONAR_TOKEN = os.getenv("SONAR_TOKEN", "")
PROJECT_KEY = os.getenv("SONAR_PROJECT_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
SEVERITIES = "BLOCKER,HIGH,MEDIUM"

PAGE_SIZE = 500

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
RECOMMENDATIONS = ("APPLY", "REVIEW_CAREFULLY", "DO_NOT_APPLY")

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "explanation": {"type": "STRING"},
        "risk_level": {"type": "STRING", "enum": list(RISK_LEVELS)},
        "behavior_change": {"type": "BOOLEAN"},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "recommendation": {"type": "STRING", "enum": list(RECOMMENDATIONS)},
        "fixed_code": {"type": "STRING"},
    },
    "required": ["explanation", "risk_level", "behavior_change",
                 "risks", "recommendation", "fixed_code"],
    "propertyOrdering": ["explanation", "risk_level", "behavior_change",
                         "risks", "recommendation", "fixed_code"],
}

def get_sonarqube_issues():
    """Fetch all open issues for the project at the configuraed severities (paginated)."""
    print("Fetching unresolved issues from SonarQube...")
    issues, page = [], 1;
    while True:
        resp = requests.get(
            f"{SONAR_URL}/api/issues/search",
            params = {
                "componentKeys": PROJECT_KEY,
                "issueStatuses": "OPEN,CONFIRMED",
                "impactSeverities": SEVERITIES,
                "ps": PAGE_SIZE,
                "p": page
            },
            auth=(SONAR_TOKEN, ""), timeout=30,
        )
        if resp.status_code != 200:
            print(f"Failed to fetch issues: {resp.status_code} {resp.text[:500]}")
            return issues
        data = resp.json()
        batch = data.get("issues",[])
        issues.extend(batch)
        total = data.get("paging", {}).get("total", data.get("total", 0))
        if not batch or len(issues) >= total:
            return issues
        page += 1

def issue_severity(issue):
    """'MAINTAINABILITY:MEDIUM' style label from the issue's impacts."""
    impacts = issue.get("impact", "")
    if not impacts:
        return "UNKNOWN"
    return ", ".join(f"{i.get('softwareQuality')}:{i.get('severity')}" for i in impacts)

def read_local_file(component):
    """'my-project:src/index.js' -> ('src/index.js', contents)."""
    path = component.split(":", 1)[-1]
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return path, f.read()
    return None, None

def build_prompt(code, issue, line, rule):
    return f"""You are a careful senior engineer fixing one SonarQube issue.

Issue: "{issue}"
Rule: {rule}
Reported near line: {line} (may have shifted slightly if the file was edited)

Full file:
<file>
{code}
</file>

Do two things:
1. Produce the complete updated file. Make the smallest change that resolves
   this one issue. Do not refactor, reformat or fix anything else.
2. Critically assess whether the change could break something. Consider:
   changed behaviour or return values, changed exceptions, public
   API/signature changes that affect callers in OTHER files you cannot see,
   edge cases (null/empty/concurrency), performance, and new imports or
   dependencies.

Respond with a single JSON object with exactly these keys:
  "explanation":     string, what you changed and why
  "risk_level":      "LOW" | "MEDIUM" | "HIGH"
  "behavior_change": boolean, true if runtime behaviour differs in any way
  "risks":           array of strings, one per concrete concern (empty if none)
  "recommendation":  "APPLY" | "REVIEW_CAREFULLY" | "DO_NOT_APPLY"
  "fixed_code":      string, the complete updated file, no markdown fences

If you cannot fix it safely with only this file visible, return the file
unchanged and recommend DO_NOT_APPLY. Be conservative: when unsure, rate the
risk higher."""

def _call_gemini(prompt, use_schema):
    config = {"temperature": 0.1, "responseMimeType": "application/json"}
    if use_schema:
        config["responseSchema"] = RESPONSE_SCHEMA
    return requests.post(
        GEMINI_URL, timeout=180,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
    )

def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):                       # strip stray markdown fences
        text = "\n".join(text.split("\n")[1:])
        text = text.rsplit("```", 1)[0]
    return json.loads(text)

def normalize(result):
    """Validate the model's answer. Anything missing or invalid defaults to the cautious value."""
    if not isinstance(result, dict) or not isinstance(result.get("fixed_code"), str):
        return None
    risk = str(result.get("risk_level", "")).upper()
    rec = str(result.get("recommendation", "")).upper()
    risks = result.get("risks")
    return {
        "fixed_code": result["fixed_code"],
        "explanation": str(result.get("explanation") or "(no explanation given)"),
        "risk_level": risk if risk in RISK_LEVELS else "HIGH",
        "behavior_change": result.get("behavior_change") is not False,
        "risks": [str(r) for r in risks] if isinstance(risks, list) else [],
        "recommendation": rec if rec in RECOMMENDATIONS else "REVIEW_CAREFULLY",
    }

def ask_gemini(code, issue, line, rule):
    """Ask Gemini for a fix plus a regression-risk assessment."""
    prompt = build_prompt(code, issue, line, rule)
    try:
        resp = _call_gemini(prompt, use_schema=True)
        if resp.status_code == 400:
            print("Gemini rejected the response schema, retrying in plain JSON mode...")
            resp = _call_gemini(prompt, use_schema=False)
        if resp.status_code != 200:
            print(f"Gemini request failed: {resp.status_code} {resp.text[:500]}")
            return None

        candidate = resp.json()["candidates"][0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            print("Gemini's answer was cut off (file too large). Skipping.")
            return None
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
        result = normalize(_parse_json(text))
        if result is None:
            print("Gemini's answer did not match the expected format. Skipping.")
        return result
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        print(f"Gemini response unusable: {e}")
        return None

def local_checks(path, old, new):
    """Cheap automatic sanity checks, independent of the model's own opinion."""
    warnings = []
    if path.endswith(".py"):
        try:
            ast.parse(new)
        except SyntaxError as e:
            warnings.append(f"New code has a Python SYNTAX ERROR: {e}")
    old_n, new_n = len(old.splitlines()), len(new.splitlines())
    changed = sum(
        1 for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
        if l[:1] in "+-" and l[:3] not in ("+++", "---")
    )
    if changed > 40:
        warnings.append(f"Large change: {changed} lines touched for a single issue.")
    if new_n < old_n * 0.8:
        warnings.append(f"File shrank from {old_n} to {new_n} lines - code may have been dropped.")
    return warnings

def show_diff(path, old, new):
    colors = {"+": "\033[32m", "-": "\033[31m", "@": "\033[36m"}
    use_color = sys.stdout.isatty()
    for l in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""):
        c = colors.get(l[:1]) if use_color else None
        print(f"{c}{l}\033[0m" if c else l)

def ask_human(risk_level, warnings):
    """Return 'y', 'n' or 'q'. High-risk changes need the word 'apply' typed out."""
    risky = risk_level == "HIGH" or bool(warnings)
    while True:
        if risky:
            ans = input("HIGH RISK. Type 'apply' to apply, [n] skip, [q] quit: ").strip().lower()
            if ans == "apply":
                return "y"
        else:
            ans = input("Apply this change? [y] yes / [n] skip / [q] quit: ").strip().lower()
            if ans in ("y", "yes"):
                return "y"
        if ans in ("", "n", "no"):
            return "n"
        if ans in ("q", "quit"):
            return "q"

def write_with_backup(path, content):
    backup = path + ".bak"
    if not os.path.exists(backup):          # keep the ORIGINAL, not an intermediate
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Applied to {path} (original saved as {backup})")


def main():
    if not SONAR_TOKEN or not GEMINI_API_KEY:
        sys.exit("Set SONAR_TOKEN and GEMINI_API_KEY environment variables first.")

    issues = get_sonarqube_issues()
    if not issues:
        print("No open issues found.")
        return
    print(f"Found {len(issues)} issue(s).\n")

    applied = skipped = 0
    for i, issue in enumerate(issues, 1):
        component, message = issue.get("component", ""), issue.get("message", "")
        line, rule = issue.get("line", "unknown"), issue.get("rule", "unknown")

        path, current = read_local_file(component)   # re-read: earlier fixes may have changed it
        if current is None:
            print(f"[{i}/{len(issues)}] No local file for {component}. Skipping.\n")
            skipped += 1
            continue

        print("=" * 78)
        print(f"[{i}/{len(issues)}] {issue_severity(issue)}  {path}:{line}")
        print(f"Issue: {message}  ({rule})")
        print("Asking Gemini...")

        result = ask_gemini(current, message, line, rule)
        if not result:
            skipped += 1
            print()
            continue

        fixed = result["fixed_code"]
        if current.endswith("\n") and not fixed.endswith("\n"):
            fixed += "\n"
        if fixed == current:
            print(f"No change proposed. Reason: {result['explanation']}\n")
            skipped += 1
            continue

        warnings = local_checks(path, current, fixed)

        print("\n--- PROPOSED CHANGE ---")
        show_diff(path, current, fixed)
        print("\n--- AI ASSESSMENT ---")
        print(f"What it does:      {result['explanation']}")
        print(f"Risk level:        {result['risk_level']}")
        print(f"Changes behaviour: {'YES' if result['behavior_change'] else 'no'}")
        print(f"Recommendation:    {result['recommendation']}")
        for r in result["risks"]:
            print(f"  - risk: {r}")
        for w in warnings:
            print(f"  ! automatic check: {w}")
        print()

        risk = "HIGH" if result["recommendation"] == "DO_NOT_APPLY" else result["risk_level"]
        decision = ask_human(risk, warnings)
        if decision == "q":
            print("Stopped by user.")
            break
        if decision == "y":
            write_with_backup(path, fixed)
            applied += 1
        else:
            print("Skipped.")
            skipped += 1
        print()

    print(f"Done. Applied: {applied}, skipped: {skipped}.")
    if applied:
        print("Next: run your tests, then re-run the Sonar scan before committing.")


if __name__ == "__main__":
    main()
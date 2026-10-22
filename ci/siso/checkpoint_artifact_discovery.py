#!/usr/bin/env python3
"""Recover the best compatible checkpoint published by an earlier CircleCI workflow."""
from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from checkpoint_catalog import rank, compatible

API = "https://circleci.com/api/v2"
JOB_RE = re.compile(r"stage-[1-5]-")
META_RE = re.compile(r"(?:^|/)checkpoint-artifact/sandium-checkpoint\.meta$")
ARCHIVE_RE = re.compile(r"(?:^|/)checkpoint-artifact/sandium-checkpoint\.tar\.zst$")

def get_json(url, token):
    req = urllib.request.Request(url, headers={"Circle-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict): raise ValueError("CircleCI returned invalid JSON object")
    return value

def pages(url, token, key):
    visited=set()
    while url:
        data=get_json(url, token)
        for item in data.get(key, []):
            if isinstance(item, dict): yield item
        nxt=data.get("next_page_token")
        if not nxt or str(nxt) in visited: break
        visited.add(str(nxt))
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"page-token":nxt})

def fetch(url, token, path):
    if urllib.parse.urlparse(url).scheme != "https": raise ValueError("non-HTTPS artifact URL rejected")
    req=urllib.request.Request(url, headers={"Circle-Token":token})
    with urllib.request.urlopen(req, timeout=180) as response, path.open("wb") as out: shutil.copyfileobj(response,out)

def run(root: Path) -> int:
    token=os.environ.get("CIRCLE_TOKEN","")
    if not token:
        print("CROSS_WORKFLOW_CHECKPOINT=SKIP_NO_TOKEN"); return 0
    branch=os.environ.get("CIRCLE_BRANCH","")
    slug=os.environ.get("CIRCLE_PROJECT_SLUG","")
    if not branch or not slug:
        print("CROSS_WORKFLOW_CHECKPOINT=SKIP_MISSING_BRANCH_OR_PROJECT"); return 0
    expected_source=os.environ.get("CIRCLE_SHA1","")
    if not expected_source:
        print("CROSS_WORKFLOW_CHECKPOINT=SKIP_MISSING_SOURCE_SHA"); return 0
    slugq=urllib.parse.quote(slug,safe="/")
    url=f"{API}/project/{slugq}/pipeline?branch={urllib.parse.quote(branch)}&page-size=100"
    candidates=[]
    for pi,pipeline in enumerate(pages(url,token,"items")):
        if pi >= 40: break
        pid=pipeline.get("id")
        if not pid: continue
        wurl=f"{API}/pipeline/{urllib.parse.quote(str(pid),safe='')}/workflow"
        for workflow in pages(wurl,token,"items"):
            wid=workflow.get("id")
            if not wid: continue
            for job in pages(f"{API}/workflow/{urllib.parse.quote(str(wid),safe='')}/job",token,"items"):
                if not JOB_RE.match(str(job.get("name",""))) or not job.get("job_number"): continue
                try:
                    arts=list(pages(f"{API}/project/{slugq}/{job['job_number']}/artifacts",token,"items"))
                except Exception as exc:
                    print(f"ARTIFACT_LIST_SKIPPED job={job.get('job_number')} reason={type(exc).__name__}")
                    continue
                meta_art=next((a for a in arts if META_RE.search(str(a.get("path",""))) and a.get("url")),None)
                arc_art=next((a for a in arts if ARCHIVE_RE.search(str(a.get("path",""))) and a.get("url")),None)
                if meta_art and arc_art: candidates.append((meta_art["url"],arc_art["url"],job.get("job_number")))
    if not candidates:
        print("CROSS_WORKFLOW_CHECKPOINT=MISS"); return 0
    root=root.resolve()
    with tempfile.TemporaryDirectory(prefix="sandium-checkpoint-candidates-",dir=root) as td:
        td=Path(td); usable=[]
        for i,(mu,au,jobno) in enumerate(candidates):
            mp,ap=td/f"{i}.meta",td/f"{i}.tar.zst"
            try:
                fetch(mu,token,mp); meta=json.loads(mp.read_text())
                if not isinstance(meta,dict) or meta.get("schema") != 1: continue
                if meta.get("source_commit") != expected_source: continue
                fetch(au,token,ap)
                if ap.stat().st_size != meta.get("archive_bytes"): continue
                if hashlib.sha256(ap.read_bytes()).hexdigest() != meta.get("archive_sha256"): continue
                if not isinstance(meta.get("progress"),dict): continue
                # The candidate's own manifest is self-consistent; build_state.py below
                # remains the authoritative current-source/GN compatibility gate.
                usable.append((meta,ap,jobno))
            except (OSError,ValueError,KeyError,urllib.error.URLError):
                continue
        usable.sort(key=lambda item:rank(item[0]),reverse=True)
        for meta,archive,jobno in usable:
            # Restore each candidate in isolation from any previous failed attempt.
            out=root/"src"/"out"/"android_Debug_arm64"
            shutil.rmtree(out,ignore_errors=True)
            shutil.copy2(archive,root/"sandium-checkpoint.tar.zst")
            manifest_source=next((mp for mp in td.glob("*.meta") if json.loads(mp.read_text()).get("archive_sha256")==meta.get("archive_sha256")),None)
            if manifest_source is None: continue
            shutil.copy2(manifest_source,root/"sandium-checkpoint.meta")
            cp=root/"src"/"brave"/"ci"/"siso"/"checkpoint.py"
            state=root/"src"/"brave"/"ci"/"siso"/"build_state.py"
            restored=subprocess.run([sys.executable,str(cp),"restore","--root",str(root)],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            if "SANDIUM_CHECKPOINT_RESTORE=PASS" not in restored.stdout: continue
            check=subprocess.run([sys.executable,str(state),"check","--mode","cache","--commit",expected_source],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            if check.returncode == 0:
                print(f"CROSS_WORKFLOW_CHECKPOINT=RESTORED job={jobno} stage={meta.get('stage')}")
                print("CROSS_WORKFLOW_CHECKPOINT_PROGRESS="+json.dumps(meta["progress"],sort_keys=True))
                return 0
            print(f"CROSS_WORKFLOW_CHECKPOINT=INCOMPATIBLE job={jobno}; trying next candidate")
            shutil.rmtree(out,ignore_errors=True)
    print("CROSS_WORKFLOW_CHECKPOINT=NO_COMPATIBLE_CANDIDATE")
    return 0

if __name__=="__main__":
    try: raise SystemExit(run(Path(os.environ.get("CIRCLE_WORKING_DIRECTORY",os.getcwd()))))
    except Exception as exc:
        print("CROSS_WORKFLOW_CHECKPOINT=API_ERROR "+str(exc),file=sys.stderr)
        raise SystemExit(0)

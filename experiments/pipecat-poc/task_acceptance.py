#!/usr/bin/env python3
"""Spoken Friday -> bash -> Pi acceptance, NOT a standalone game generator.

This harness only synthesizes user requests, observes files/HTTP, and tests the
result in an isolated browser. It never invokes Pi or writes application code.
Requires the repository interpreter with Playwright and the POC environment.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess

from baseline import HERE, check_report
from poc_report import atomic_json


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def acceptance(mode):
    from playwright.async_api import async_playwright
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = HERE/'.fixtures'/('voice-task-'+stamp)
    workdir = folder/'documents'
    workdir.mkdir(parents=True)
    port = available_port()
    url = f'http://127.0.0.1:{port}/'
    original_ready, changed_ready = folder/'original-ready', folder/'changed-ready'
    pi_hint = ('Use Pi in print mode with extensions and MCP disabled, using the '
               'OpenRouter model openai/gpt-4.1-mini. Delegate all coding to Pi, do not write it yourself. ')
    specs = [
        ('pi-start', pi_hint+'Ask Pi to build index.html, a simple browser click counter game '
         'with buttons labelled Click me and Reset, and visible text Score colon zero. '
         f'Then serve it on localhost port {port}. Run this as a managed background job and return immediately.',
         None, ['background_start'], True),
        ('while-pi-works', 'Meanwhile, what is seven plus five? Answer briefly.', None, [], False),
        ('pi-link', 'Check the background job and give me the game link if ready.', original_ready, ['job_status'], False),
        ('pi-change', 'Stop the current server job. '+pi_hint+'Ask Pi to rename the page title '
         'to Friday Space Game and add Space key control to increment the score once per press. '
         'Ask Pi to validate the JavaScript syntax and keep click and reset working. '
         f'Then serve it again on localhost port {port} as a managed background job.',
         None, ['job_cancel', 'background_start'], True),
        ('pi-changed-link', 'Check the background job and tell me the updated game link.', changed_ready, ['job_status'], False),
    ]
    synth = shutil.which('espeak-ng') or shutil.which('espeak')
    if not synth:
        raise RuntimeError('espeak is required; no install attempted')
    cases = []
    for name, question, marker, tools, uses_pi in specs:
        wav = folder/(name+'.wav')
        subprocess.run([synth, '-s', '165', '-w', str(wav), question], check=True)
        cases.append({'id': name, 'question': question, 'wav': wav.name,
            'wait_for_signal': str(marker) if marker else None,
            'expected_tools': tools, 'expect_pi_delegation': uses_pi,
            'expected_job_status': 'running',
            'expected_any': ['twelve', '12'] if name == 'while-pi-works' else []})
    plan = folder/'plan.json'
    atomic_json(plan, {'source': 'synthetic spoken task acceptance, not natural speech quality',
        'allow_interruptions': True, 'minimum_duration_s': 0, 'cases': cases})
    receipt = {'kind': 'voice_pi_task_acceptance', 'mode': mode,
        'harness_invokes_pi': False, 'harness_writes_game_code': False,
        'pi_model_requested': 'openai/gpt-4.1-mini', 'pi_api_cost_usd': None,
        'checks': [], 'human_quality_review': 'pending', 'server_left_running': False}
    previous = set((HERE/'results').glob('session-*.json'))
    log = HERE/'results/logs'/('voice-task-'+stamp+'.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    child = None
    try:
        with log.open('w') as stream:
            child = await asyncio.create_subprocess_exec(str(HERE/'run.sh'), mode,
                '--plan', str(plan), '--folder', str(workdir), cwd=HERE,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            async with async_playwright() as pw:
                executable = Path(pw.chromium.executable_path)
                if not executable.exists():
                    installed = sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux64/chrome'))
                    if not installed:
                        raise RuntimeError('No installed Chromium; no automatic install attempted')
                    executable = installed[-1]
                browser = await pw.chromium.launch(headless=True, executable_path=str(executable))
                try:
                    page = await browser.new_page()
                    await page.route('**/*', lambda route: route.continue_()
                        if route.request.url.startswith(url) else route.abort())
                    async def score():
                        match = re.search(r'Score\s*:\s*(\d+)', await page.locator('body').inner_text(), re.I)
                        if not match:
                            raise AssertionError('No visible Score: number')
                        return int(match[1])
                    async def check_game(changed=False):
                        deadline, last_error = asyncio.get_running_loop().time()+150, 'server not ready'
                        while asyncio.get_running_loop().time() < deadline:
                            if child.returncode is not None:
                                raise RuntimeError('Friday exited before task/browser acceptance')
                            try:
                                response = await page.goto(url, timeout=1500)
                                assert response and response.status == 200
                                if changed:
                                    assert await page.title() == 'Friday Space Game'
                                await page.get_by_role('button', name=re.compile('Reset', re.I)).click(timeout=1000)
                                assert await score() == 0
                                await page.get_by_role('button', name=re.compile('Click me', re.I)).click(timeout=1000)
                                assert await score() == 1
                                await page.get_by_role('button', name=re.compile('Reset', re.I)).click(timeout=1000)
                                if changed:
                                    await page.locator('body').click(position={'x': 1, 'y': 1})
                                    await page.keyboard.press('Space')
                                    assert await score() == 1
                                return
                            except Exception as exc:
                                last_error = f'{type(exc).__name__}: {exc}'[:400]
                                await asyncio.sleep(.3)
                        raise RuntimeError('Browser acceptance timed out: '+last_error)
                    await check_game()
                    receipt['checks'].append({'check': 'spoken-create-Pi-launch-browser-click-reset', 'passed': True})
                    original_ready.touch()
                    await check_game(changed=True)
                    receipt['checks'].append({'check': 'spoken-change-Pi-relaunch-browser-space-control', 'passed': True})
                    changed_ready.touch()
                    await asyncio.wait_for(child.wait(), 90)
                finally:
                    await browser.close()
        reports = sorted(set((HERE/'results').glob('session-*.json'))-previous)
        receipt['reports'] = [p.name for p in reports]
        receipt['voice_fixture_checks_passed'] = bool(reports) and all(check_report(json.loads(p.read_text())) for p in reports)
        receipt['exit_code'] = child.returncode
        if child.returncode or not receipt['voice_fixture_checks_passed']:
            raise RuntimeError('Voice/tool/background-state checks failed; inspect retained report')
        receipt['passed'] = True
    except Exception as exc:
        receipt.update(passed=False, failure=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if child and child.returncode is None:
            os.killpg(child.pid, signal.SIGINT)
            try:
                await asyncio.wait_for(child.wait(), 20)
            except asyncio.TimeoutError:
                os.killpg(child.pid, signal.SIGKILL)
                await child.wait()
                receipt['forced_shutdown'] = True
        receipt['reports'] = [p.name for p in sorted(set((HERE/'results').glob('session-*.json'))-previous)]
        receipt['log'] = str(log.relative_to(HERE))
        output = HERE/'results'/('voice-task-'+stamp+'.json')
        atomic_json(output, receipt)
        print('Task acceptance receipt: '+str(output), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['realtime', 'audio', 'separate'], default='separate')
    asyncio.run(acceptance(parser.parse_args().mode))


if __name__ == '__main__':
    main()

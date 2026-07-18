<div align="center">

# AI Desktop Usage Monitor

**Codex와 Claude Desktop 사용량을 화면 구석에서 작게 확인하는 Windows용 미니 HUD**

작업 중에는 미니 HUD로 사용량만 빠르게 보고, 자세한 정보가 필요하면 더블클릭해서 전체 HUD로 전환하세요.

<p>
  <a href="#실행-방법"><img alt="Windows" src="https://img.shields.io/badge/Windows-Desktop-0078D4?style=for-the-badge&logo=windows&logoColor=white"></a>
  <a href="#데이터-출처"><img alt="Local first" src="https://img.shields.io/badge/LOCAL-FIRST-2DD4BF?style=for-the-badge"></a>
  <a href="#공개보안-주의사항"><img alt="No network" src="https://img.shields.io/badge/NO_NETWORK-111827?style=for-the-badge"></a>
  <a href="#english"><img alt="English" src="https://img.shields.io/badge/English-available-6366F1?style=for-the-badge"></a>
</p>

<p>
  <a href="#실행-방법">실행 방법</a> ·
  <a href="#경로-설정">경로 설정</a> ·
  <a href="#ui-사용법">화면 보기</a> ·
  <a href="#english">English</a>
</p>

</div>

---

## 화면 미리보기

| 전체 HUD | 미니 HUD |
| --- | --- |
| ![Full HUD](docs/screenshots/full-hud-capture.png) | ![Mini HUD](docs/screenshots/mini-hud-capture.png) |

| 임계치 색상 |
| --- |
| ![Threshold colors](docs/screenshots/threshold-colors.png) |

## 표시하는 정보

- ChatGPT / Codex 로컬 세션 로그 기반 사용량
- Claude Desktop 로컬 사용량 캐시 기반 사용량
- 항상 위에 표시 가능한 작은 HUD
- 아이콘 + 퍼센트만 보이는 미니 HUD
- 투명도 조절, 수동 새로고침, 50% / 90% 임계치 색상 표시

## 데이터 출처

이 앱은 로컬 파일과 Windows 접근성 텍스트만 읽습니다. OpenAI, Anthropic 또는 외부 API로 요청을 보내지 않습니다.

| 대상 | 읽는 위치 | 신뢰도 |
| --- | --- | --- |
| ChatGPT / Codex | `~/.codex/sessions/**/*.jsonl` 안의 `rate_limits` | 높음 |
| Claude Desktop | Claude Desktop의 `plan-usage-history.json` | 중간 |
| Claude fallback | Claude Desktop 로그와 Windows UI Automation | 중간 / 낮음 |

Claude Desktop의 로컬 사용량 캐시 구조는 Anthropic이 공식 공개한 API가 아닙니다. 현재 앱은 내부 키를 다음처럼 해석합니다.

- `fh`: 5시간 사용량
- `sd`: Sonnet 관련 사용량 신호
- `wk` / `7d`: 주간 사용량, 값이 있을 때

Claude 캐시가 최근에 갱신되지 않았으면 실제 현재값처럼 표시하지 않고 `stale`로 표시합니다.

## 실행 방법

PowerShell에서 실행:

```powershell
.\run_monitor.ps1
```

수집값만 확인:

```powershell
.\run_monitor.ps1 -Once
```

JSON 출력:

```powershell
.\run_monitor.ps1 -Once -Json
```

Python은 다음 순서로 자동 탐색합니다.

1. `AI_USAGE_MONITOR_PYTHON` 환경변수
2. 로컬 `.venv\Scripts\python.exe`
3. Codex 번들 런타임 `~\.cache\codex-runtimes\...`
4. `py -3`
5. `python`

Python이 자동으로 잡히지 않으면 아래처럼 직접 지정할 수 있습니다.

```powershell
$env:AI_USAGE_MONITOR_PYTHON = "C:\Path\To\python.exe"
.\run_monitor.ps1
```

`AI Usage Monitor.vbs`는 콘솔 창 없이 실행하기 위한 런처입니다. Windows 바로가기 아이콘을 쓰고 싶다면 `.vbs`를 가리키는 `.lnk`를 각자 로컬에서 새로 만드는 것을 권장합니다. `.lnk`는 절대 경로를 포함할 수 있으므로 GitHub에 올리지 않는 편이 좋습니다.

## 경로 설정

기본 Windows Desktop 환경에서는 별도 경로 설정이 필요 없습니다.

앱은 현재 Windows 사용자 계정 기준으로 아래 경로를 자동 확인합니다.

| 대상 | 기본 경로 |
| --- | --- |
| ChatGPT / Codex | `%USERPROFILE%\.codex\sessions\**\*.jsonl` |
| Claude Desktop | `%USERPROFILE%\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\plan-usage-history.json` |
| Claude 로그 fallback | `%USERPROFILE%\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\logs\*.log` |

앱에서 `missing`이 보이면 다음을 확인하세요.

1. Codex 또는 Claude Desktop을 해당 Windows 계정에서 한 번 이상 실행했는지
2. Codex 로컬 세션 로그에 `rate_limits`가 생성되었는지
3. Claude Desktop이 `plan-usage-history.json`을 생성하거나 갱신했는지
4. Codex / Claude Desktop을 사용하는 Windows 계정과 같은 계정에서 모니터를 실행 중인지

현재는 커스텀 로그 경로를 CLI 옵션으로 받지 않습니다. 앱이 비표준 위치에 데이터를 저장한다면 `app.py`의 `collect_codex_usage()` 또는 `collect_claude_usage()`에서 경로 생성 부분을 수정해야 합니다.

## UI 사용법

- `PIN`: 항상 위에 표시 토글
- 투명도 슬라이더: 창을 덜 방해되게 조절
- 새로고침 아이콘: 즉시 갱신
- `MIN`: 미니 HUD로 전환
- 미니 HUD 드래그: 위치 이동
- 미니 HUD 더블클릭: 전체 HUD로 복귀

사용량 색상은 다음 기준으로 바뀝니다.

- `50% 미만`: 기본 색상
- `50% 이상`: 경고 색상
- `90% 이상`: 위험 색상

## 공개/보안 주의사항

이 앱은 로컬 우선으로 동작합니다.

- credential, cookie, auth 파일, screenshot, OCR을 읽지 않습니다.
- 외부 네트워크 요청을 보내지 않습니다.
- 사용량 관련 로컬 로그/캐시 파일만 읽습니다.
- CLI 출력에서는 로컬 사용자명이 드러나지 않도록 경로를 `~`로 축약합니다.

GitHub에 올릴 때는 다음 파일을 커밋하지 마세요.

- `AI Usage Monitor.lnk`
- `.agents/`
- `.codex/`
- `.venv/`
- `assets/chatgpt.png`, `assets/claude.png` 같은 로컬 provider 로고
- 로그, `.env`, 개인 스크린샷, 로컬 캐시 파일

포함된 앱 아이콘과 새로고침 아이콘은 프로젝트에서 생성한 로컬 자산입니다. Provider 로고가 로컬에 있으면 편의상 표시할 수 있지만, 재배포 권한이 없다면 GitHub에 포함하지 않는 것을 권장합니다.

## 한계

- Claude Desktop 값은 내부 로컬 캐시/로그 포맷에 의존하므로 앱 업데이트에 따라 바뀔 수 있습니다.
- Claude UI fallback은 best-effort입니다. 일부 Claude Desktop 화면은 Windows 접근성 API로 사용량 텍스트를 노출하지 않습니다.
- Codex 사용량은 `rate_limits`가 포함된 로컬 Codex 세션 로그가 있어야 표시됩니다.
- 이 앱은 공식 billing 또는 quota API 클라이언트가 아니라 개인용 데스크톱 모니터입니다.

## Repository 체크리스트

- 코드를 다른 사람이 재사용해도 된다면 MIT 같은 license를 추가하세요.
- 첫 커밋 전에 `.gitignore`를 유지하세요.
- 소스 파일과 직접 생성한 프로젝트 아이콘만 커밋하세요.
- 로컬 바로가기, 복사한 provider 로고, 로그, 캐시, 개인 스크린샷, 가상환경은 커밋하지 마세요.
- Claude 지원이 공식 API가 아니라 내부 캐시 기반이라는 점을 명확히 적어두세요.

---

# English

Small Windows desktop HUD for local Codex and Claude Desktop usage signals.

It is designed as a lightweight glanceable monitor: keep the tiny strip near the edge of your screen, then double-click it when you need the full HUD.

## What it shows

- ChatGPT / Codex usage from local Codex session logs
- Claude Desktop usage from Claude's local usage cache
- Compact always-on-top HUD
- Tiny titlebar-free mode with icon + percentage
- Opacity control, manual refresh, and 50% / 90% threshold colors

## Data sources

This app only reads local files and Windows accessibility text. It does not call OpenAI, Anthropic, or any external API.

| Provider | Source | Confidence |
| --- | --- | --- |
| ChatGPT / Codex | `~/.codex/sessions/**/*.jsonl`, extracting `rate_limits` | High |
| Claude Desktop | Claude Desktop `plan-usage-history.json` | Medium |
| Claude fallback | Claude Desktop logs and Windows UI Automation when available | Medium / Low |

Claude Desktop's local usage schema is not a public Anthropic API. The app currently treats:

- `fh` as the five-hour usage window
- `sd` as a Sonnet-related usage signal
- `wk` / `7d` as weekly usage, when present

If Claude has not refreshed its local cache recently, the monitor marks the value as stale instead of pretending it is live.

## Run

PowerShell:

```powershell
.\run_monitor.ps1
```

Collector-only smoke test:

```powershell
.\run_monitor.ps1 -Once
```

JSON output:

```powershell
.\run_monitor.ps1 -Once -Json
```

Python discovery order:

1. `AI_USAGE_MONITOR_PYTHON`, if set
2. local `.venv\Scripts\python.exe`
3. Codex bundled runtime under `~\.cache\codex-runtimes\...`, when available
4. `py -3`
5. `python`

If Python is not detected automatically, set:

```powershell
$env:AI_USAGE_MONITOR_PYTHON = "C:\Path\To\python.exe"
.\run_monitor.ps1
```

`AI Usage Monitor.vbs` is available as a hidden-console launcher. For a custom Windows icon, create a local shortcut to that `.vbs` file instead of committing a `.lnk` file.

## Path configuration

No provider path setup is required for the default Windows desktop setup.

The monitor automatically checks these locations under the current Windows user profile:

| Provider | Default path |
| --- | --- |
| ChatGPT / Codex | `%USERPROFILE%\.codex\sessions\**\*.jsonl` |
| Claude Desktop | `%USERPROFILE%\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\plan-usage-history.json` |
| Claude logs fallback | `%USERPROFILE%\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\logs\*.log` |

If the monitor shows `missing`, check that:

1. Codex or Claude Desktop has been run at least once on that Windows account.
2. Codex has created local session logs containing `rate_limits`.
3. Claude Desktop has created or refreshed `plan-usage-history.json`.
4. You are running the monitor under the same Windows user account that uses Codex / Claude Desktop.

Currently, custom provider log paths are not exposed as command-line options. If your apps store data in a non-standard location, update the path construction in `collect_codex_usage()` or `collect_claude_usage()` in `app.py`.

## UI

The monitor uses a simple dark rounded-card HUD layout.

![Full HUD](docs/screenshots/full-hud-capture.png)

- `PIN`: toggles always-on-top
- opacity slider: makes the window less intrusive
- refresh icon: updates immediately
- `MIN`: switches to the tiny titlebar-free strip
- tiny strip drag: moves the strip
- tiny strip double-click: returns to the full HUD

![Mini HUD](docs/screenshots/mini-hud-capture.png)

Usage colors escalate at:

- `< 50%`: normal provider color
- `>= 50%`: warning color
- `>= 90%`: critical color

![Threshold colors](docs/screenshots/threshold-colors.png)

## Privacy and security

The app is intentionally local-first:

- No credentials, cookies, auth files, screenshots, or OCR are read.
- No network requests are made by the monitor.
- The app reads only usage-related local logs/cache files.
- CLI output shortens source paths with `~` to avoid exposing the local username.

Before publishing or forking, do not commit local-only artifacts:

- `AI Usage Monitor.lnk`
- `.agents/`
- `.codex/`
- `.venv/`
- copied provider icons such as `assets/chatgpt.png` or `assets/claude.png`
- logs, `.env`, personal screenshots, or local cache files

The included app icon and refresh icon are project-local generated assets. Provider logos, if present locally, are only used as local convenience assets and should not be redistributed unless you have the right to do so.

## Limitations

- Claude Desktop values depend on internal local cache/log formats and may change.
- The Claude UI fallback is best-effort; some views do not expose usage text through Windows accessibility APIs.
- Codex usage depends on local Codex session logs containing `rate_limits`.
- This is a personal desktop monitor, not an official billing or quota API client.

## Suggested repository checklist

- Add a license, for example MIT, if you want others to reuse the code.
- Keep `.gitignore` intact before the first commit.
- Commit source files and generated project icons only.
- Do not commit local shortcuts, copied provider logos, logs, caches, personal screenshots, or virtual environments.
- Mention clearly that Claude support is unofficial and cache-based.

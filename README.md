# Rich Prompt

YAML/JSON 설정 하나를 **Bash, tcsh, Zsh, PowerShell(pwsh)** 프롬프트 source 파일로 변환합니다. Python은 생성할 때만 필요하며, 프롬프트 표시 시에는 생성된 셸 코드와 설정에 적은 명령만 실행됩니다.

변환기와 CLI 이름은 `promptgen`입니다. RGB 색상, 정적 그라데이션, 여러 줄 프롬프트, 셸별 동적 표현식과 Powerline 스타일 연결을 지원합니다.

## 빠른 시작

Python 3.10 이상에서 추가 패키지 없이 JSON 예제를 사용할 수 있습니다.

```sh
git clone https://github.com/E1der0wL/rich-prompt.git
cd rich-prompt
python promptgen.py prompt.example.json --shell all --output-dir generated
```

예제 설정은 [JSON](prompt.example.json)과 [YAML](prompt.example.yaml)로 제공됩니다. `generated/`의 파일은 설정에서 재생성하는 결과물이므로 Git으로 추적하지 않습니다.

| 셸 | 생성 파일 | 지원 기준 |
| --- | --- | --- |
| Bash | `generated/prompt.generated.bash` | Bash 4.4 이상 |
| tcsh | `generated/prompt.generated.tcsh` | tcsh 6.24 이상 |
| Zsh | `generated/prompt.generated.zsh` | Zsh 5.x |
| PowerShell | `generated/prompt.generated.ps1` | PowerShell 7 이상 (`pwsh`) |

macOS 기본 Bash 3.2와 Windows PowerShell 5.1은 지원 대상이 아닙니다.

YAML과 복합 이모지 폭 계산을 함께 사용하려면 프로젝트 폴더에서 설치합니다.

```sh
python -m pip install -e '.[all]'
promptgen prompt.example.yaml --shell all --output-dir generated
```

선택 설치: `.[yaml]`은 PyYAML, `.[unicode]`는 wcwidth/regex를 설치합니다. 설치 없이 `python promptgen.py`로 실행해도 됩니다. Powerline 모양 문자(``, ``)가 깨지면 해당 문자를 지원하는 폰트를 사용하거나 `prefix`/`suffix`를 일반 문자로 바꾸세요.

## 설정 만들기와 생성

```sh
# 새 설정 생성 — 기존 파일은 덮어쓰지 않음
python promptgen.py --init prompt.json
python promptgen.py --init prompt.yaml

# 선택한 셸만 생성
python promptgen.py prompt.json --shell zsh -o generated/prompt.generated.zsh
python promptgen.py prompt.json --shell pwsh -o generated/prompt.generated.ps1

# 네 셸의 설정 검증 — 출력 파일을 쓰지 않음
python promptgen.py prompt.json --shell all --check

# 네 파일을 한 번에 생성
python promptgen.py prompt.json --shell all --output-dir generated
```

`--check`는 설정 형식·폭·색상·선택한 셸의 표현식 유무를 검증합니다. `eval`의 셸 문법을 실행하거나 검사하지는 않습니다. `--shell all`은 모든 대상의 설정 검증과 코드 생성을 마친 뒤 파일을 기록합니다. 파일마다 임시 파일과 교체 방식을 사용합니다. 기존 생성 파일은 갱신하지만 설정 파일 자체를 출력 대상으로 지정할 수는 없습니다.

## 셸에 적용

아래는 생성 파일을 `~/.config/promptgen/`에 두었을 때의 예입니다. 실제 생성 위치에 맞게 경로를 바꾸세요. 설정을 편집한 뒤 다시 생성하고 source하면 변경 사항이 적용됩니다.

**Bash — `~/.bashrc`**

```bash
source ~/.config/promptgen/prompt.generated.bash
```

기존 문자열/배열 `PROMPT_COMMAND`를 보존하고 생성 함수가 먼저 실행되도록 등록합니다. 반복 source해도 중복 등록하지 않습니다. 기존 훅에 이전 명령의 종료 코드를 전달합니다. 안전한 간접 프롬프트 확장을 위해 `promptvars`를 활성화합니다.

**Zsh — `~/.zshrc`**

```zsh
source ~/.config/promptgen/prompt.generated.zsh
```

`precmd_functions`에 중복 없이 등록합니다. 기존 `precmd`와 다른 훅을 보존합니다. `promptsubst`, `promptpercent`를 켜고 `promptbang`을 끕니다. 별도의 `precmd` 함수는 배열 훅보다 먼저 실행되므로 해당 함수가 종료 상태를 바꾸면 `PROMPTGEN_STATUS`에도 그 결과가 반영됩니다.

**tcsh — `~/.tcshrc`**

```csh
set promptgen_source = "$HOME/.config/promptgen/prompt.generated.tcsh"
source "$promptgen_source"
```

tcsh는 매 프롬프트 전에 생성 파일을 다시 source합니다. `promptgen_source`를 지정하지 않으면 첫 source 당시 현재 디렉터리에서 생성 파일 이름을 찾습니다. 파일을 이동하거나 이름을 바꾸면 위 변수를 갱신하세요. **기존 `precmd` alias는 교체합니다.** 여러 줄은 tcsh 6.24 이상의 C-style 문자열로 생성합니다.

**PowerShell — `$PROFILE`**

```powershell
. "$HOME/.config/promptgen/prompt.generated.ps1"
```

앞의 점(`.`)은 dot-source 연산자입니다. 현재 세션의 전역 `prompt` 함수를 정의하므로 **기존 prompt 함수는 교체합니다.** 생성 파일을 다시 dot-source하면 새 설정이 적용됩니다.

## 설정 형식

```yaml
version: 1
segments:
  - prefix: "["
    suffix: "]"
    content:
      eval:
        bash: '\\u@\\h'
        tcsh: '%n@%m'
        zsh: '%n@%m'
        pwsh: '$([Environment]::UserName)@$([Environment]::MachineName)'
    fg: "255,255,255"
    bg: "45,90,160"
    effects: [bold]
  - newline
  - content:
      text: "❯ "
    fg: "100,220,150"
```

| 항목 | 의미 |
| --- | --- |
| `content.text` | 그대로 표시할 문자열. `content: "문자열"`로 줄여 쓸 수 있음 |
| `content.eval` | 매 프롬프트마다 평가하는 셸별 표현식 |
| `prefix`, `suffix` | 앞뒤 장식 문자. 기본값은 빈 문자열 |
| `space_before`, `space_after` | 앞뒤 공백 개수. 0 이상의 정수 |
| `fg`, `bg` | RGB `"r,g,b"` 또는 그라데이션 `"r,g,b - r,g,b"` |
| `effects` | `bold`, `dim`, `italic`, `underline`, `blink`, `reverse`, `hidden`, `strike` |
| `width` | 장식·공백을 포함한 전체 표시 셀 폭. 정적 문자열만 지원 |
| `hide_empty` | 내용이 빈 문자열이면 장식·공백까지 숨김. 기본값 `false` |
| `connect: previous` | 같은 줄의 직전 표시 항목과 연결. 숨겨진 항목은 건너뜀 |
| `newline` | 다음 줄 시작. `- newline` 또는 `- {newline: true}` |

`width`는 긴 문자열을 잘라내고 짧은 문자열에 공백을 붙입니다. 그라데이션은 `width`가 있는 정적 문자열에서만 지원합니다. `connect: previous`는 이전 항목의 suffix를 이전 배경색/현재 배경색으로 칠하고 현재 prefix를 생략합니다.

### 동적 표현식

`content.eval`은 대상 셸의 **큰따옴표 문자열 내부**에 들어갑니다. 변수/명령 치환 문법과 필요한 인용은 해당 셸을 따릅니다. 명령문 블록 전체를 넣는 방식은 아닙니다. Bash의 `\\u`, tcsh/Zsh의 `%n` 같은 네이티브 프롬프트 코드도 사용할 수 있습니다.

```yaml
content:
  eval:
    bash: '$(git branch --show-current 2>/dev/null)'
    tcsh: '`git branch --show-current`'
    zsh: '$(git branch --show-current 2>/dev/null)'
    pwsh: '$(git branch --show-current 2>$null)'
hide_empty: true
```

이 예제는 Git이 설치된 저장소 디렉터리에서 사용합니다. 표현식의 오류 처리도 셸별로 직접 지정합니다. tcsh에서 역따옴표 명령 출력의 개행은 셸의 단어 분리 규칙을 따릅니다.

`content.eval`을 단일 문자열로 적거나 맵의 `default`를 지정할 수도 있습니다. 여러 셸에서 문법이 같은 경우에만 사용하세요. 선택한 셸 키와 `default`가 모두 없으면 검증에 실패합니다. `eval`은 설정 작성자가 허용한 명령을 실행하므로 직접 작성하거나 신뢰하는 설정에 사용하세요.

`content.text`와 장식 문자열의 `$`, 역따옴표, 네이티브 프롬프트 코드는 리터럴로 처리합니다. Bash/Zsh는 `eval`에서 얻은 명령 출력에 들어 있는 `$()`를 다시 실행하지 않습니다. 제어 문자를 직접 설정에 넣는 대신 줄바꿈 항목과 색상 옵션을 사용하세요.

### 이전 명령 상태

| 셸 | 변수 |
| --- | --- |
| Bash, Zsh | `$PROMPTGEN_STATUS` — 훅 진입 시 종료 코드 |
| tcsh | `$promptgen_status` — 갱신 시작 시 `$status` |
| PowerShell | `$PROMPTGEN_STATUS` — 성공 0 / 실패 1 |
| PowerShell | `$PROMPTGEN_SUCCESS` — 이전 `$?`의 boolean 값 |
| PowerShell | `$PROMPTGEN_LASTEXITCODE` — 마지막 외부 프로그램의 종료 코드 |

PowerShell의 `$LASTEXITCODE`는 cmdlet 실행 후에도 오래된 값이 남을 수 있어 성공 여부와 구분합니다. 프롬프트 표현식이 외부 명령을 실행해도 기존 `$LASTEXITCODE`는 복원합니다.

## 테스트

```sh
python -m unittest discover -s tests -v
```

설정 검증·파일 생성·Unicode 폭·문자열 인용·빈 항목 연결·여러 줄·동적 표현식·훅 중복 등록·종료 상태를 검증합니다. 실행 환경에서 찾을 수 없는 셸은 해당 테스트를 건너뜁니다. Windows에서는 pwsh 테스트, Linux/WSL에서는 Bash/tcsh/Zsh 테스트를 실행할 수 있습니다.

화면 폭은 터미널과 폰트에 따라 차이가 있을 수 있습니다. 복합 이모지를 쓴다면 `.[unicode]` 설치를 권장합니다. Python/awk/sed를 프롬프트마다 실행하지 않도록 동적 항목의 `width`와 그라데이션은 제한합니다.

구현 참고: [Bash 프롬프트 확장](https://www.gnu.org/software/bash/manual/html_node/Controlling-the-Prompt.html), [Zsh 프롬프트 확장](https://zsh.sourceforge.io/Doc/Release/Prompt-Expansion.html), [PowerShell prompt 함수](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_prompts).

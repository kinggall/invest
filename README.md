# invest

한국투자증권 모의투자 전략을 실행하고 주문·체결을 확인하는 독립 프로젝트입니다. 기존 `kinggall/domino`의 overview와 코드는 변경하지 않습니다. Python 3.10 이상으로 실행하며 외부 패키지는 필요 없습니다.

## Windows 설치와 실행

PowerShell에서 실행합니다. Git이 설치되어 있어야 합니다.

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\projects" | Out-Null
git clone https://github.com/kinggall/invest.git "$env:USERPROFILE\projects\invest"
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\projects\invest\start-dashboard.ps1"
```

브라우저에서 **http://127.0.0.1:8766**이 열립니다. ExecutionPolicy 옵션은 이 프로세스에만 적용하며 PC의 정책을 변경하지 않습니다.

실행 스크립트는 명시한 `-DominoHome`, 기존 `DOMINO_HOME`, 저장된 데이터 폴더 순으로 선택합니다. 모두 미설정이면 사용자 `projects/domino-code` 및 `C:/domino`에 `kis_config.json`이 있는지 확인합니다. 찾지 못하면 **기존 설정·토큰이 저장된 데이터 폴더**를 PC에서 입력받습니다. 기존 코드가 있는 폴더와 데이터 폴더는 다를 수 있습니다. 키나 토큰 문자열을 입력하는 절차가 아닙니다.

모의계좌 번호는 최초 실행 시 PC에서 하이픈 없는 8자리를 입력합니다. 데이터 폴더·모의계좌 번호·상품코드는 `%LOCALAPPDATA%/invest/settings.json`에 저장하며 다음 실행과 진단에서 자동 재사용합니다. 이 파일은 Git 저장소 밖에 있으며 키·시크릿·토큰은 저장하지 않습니다. 상품코드는 `01`이 기본값입니다. 다른 상품코드가 필요하면 실행 전에 `$env:KIS_DEMO_PRODUCT`에 지정하세요. 계좌·키·토큰을 채팅이나 GitHub에 올리지 마세요.

데이터 폴더를 명시하려면:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\projects\invest\start-dashboard.ps1" -DominoHome 'D:\실제데이터폴더'
```

페이지에서 **전략 저장 → 시세·조건 확인 → 모의 자동매매 시작** 순서로 사용합니다. 실행 중에는 전략 변경을 잠급니다. Python 서버를 재시작하면 항상 중지 상태로 시작합니다.

## 관리 페이지

- 전략: 종목코드, 매수·매도, 수량, 기준 가격, 지정가, 주문금액 상한.
- 실행: 저장된 전략을 10초마다 평가하고 조건 충족 시 모의 주문 전송.
- 조회: 이 프로젝트에서 기록한 최근 100건의 주문·부분체결·전체체결·취소 상태.
- 연결 상태: 기존 demo 토큰, 모의계좌 설정, 최근 실행 결과.

매수는 현재가가 기준 가격 이하, 매도는 이상일 때 지정가 주문을 전송합니다. 주문금액 상한은 지정가×수량의 주문 1건 상한입니다. 예시 전략의 가격은 사용법 설명용이므로 직접 수정하세요. 같은 계좌·한국 날짜·전략 ID는 같은 DB에서 하루 한 번만 주문합니다.

페이지를 닫아도 Python 서버가 실행 중이면 자동매매는 계속됩니다. **중지 버튼은 다음 전략 실행을 멈추며, 이미 전송된 미체결 주문을 취소하지 않습니다.** 통신·토큰 오류가 나면 자동 실행을 중지합니다. 취소·정정은 이 버전에서 지원하지 않습니다.

계좌 전체 잔고·수익률·다른 프로그램의 주문은 수집하지 않습니다. 국내주식 KRX 지정가 모의투자 전용이며, 해외주식·실전매매는 지원하지 않습니다. 주문가능시간·호가단위·잔고·매도가능수량은 한투가 최종 검증합니다.

## 토큰 중복 발급 방지

**invest에는 토큰 발급 기능이 없습니다.** 기존 PC 데이터 폴더의 `kis_config.json` demo 프로필과 `kis_token_demo.json`을 읽기만 합니다. 기존 프로그램의 기본 설정이 real이어도 demo만 선택합니다. 실전 호스트·실전 TR·토큰 발급 경로는 허용하지 않습니다.

요청마다 demo+앱키+시크릿의 SHA256 binding과 만료시간을 확인합니다. 토큰 만료 120초 전부터 중단하며 기존 설정·토큰 파일을 쓰거나 삭제하지 않습니다. 기존 프로그램이 토큰을 갱신하면 다음 요청부터 반영합니다. 키/토큰은 브라우저에 전달하지 않습니다.

유효한 demo 토큰이 없다면 기존 domino에서 준비해야 합니다. 확인된 기존 코드 위치는 `C:/Users/shoh7/projects/domino-code/domino_kis.py`입니다. 기존 데이터 폴더가 `DOMINO_HOME`으로 올바르게 지정된 동일 PC 환경에서 실행합니다:

```powershell
python "$env:USERPROFILE\projects\domino-code\domino_kis.py" check --environment demo
```

demo 프로필 자체가 없다면 기존 프로그램의 `configure --environment demo`로 PC에서 먼저 설정하세요. 실전 키와 모의 키는 서로 다릅니다. 토큰 발급 주체는 기존 domino 하나로 유지합니다. 기존 인증 코드 자체에는 프로세스 간 잠금이 없으므로 기존 인증 명령 여러 개를 동시에 실행하지 마세요. 기존 프로그램과 invest의 호출량은 계정 단위로 합산됩니다. invest는 호출 간 최소 0.6초 간격을 둡니다.

기존과 같은 `KIS_ENV=demo`, `KIS_APP_KEY`, `KIS_APP_SECRET` 쌍을 사용하면 같은 binding을 검사합니다. 환경변수만 쓰더라도 기존 demo 프로필 파일을 준비해야 합니다.

## 기록 보존과 주문 불확실성

기본 DB는 프로젝트의 `runtime/orders.sqlite3`입니다. 실행 스크립트는 작업 폴더에 관계없이 이 경로를 고정합니다. CLI에서도 같은 DB를 사용하세요. DB를 삭제하거나 다른 DB·다른 PC에서 실행하면 중복 방지가 공유되지 않습니다. 서버는 한 개만 실행하세요.

주문 전 SQLite에 UNKNOWN 의도를 커밋합니다. 응답이 끊기거나 불명확해도 자동 재주문하지 않습니다. UNKNOWN은 페이지에서 ‘접수 확인 필요’로 표시합니다. 실제 한투 모의 주문내역을 확인한 뒤 다음 명령으로 주문번호를 연결할 수 있습니다:

```powershell
Set-Location "$env:USERPROFILE\projects\invest"
python trader.py --domino-home 'D:\실제데이터폴더' reconcile --strategy-id samsung-limit-v1 --day 20261116 --order-no 0000123456 --branch 001
```

명령의 날짜·ID·번호·지점은 실제 주문으로 바꾸세요. 조회된 주문일·종목·주문번호·매수매도·수량·가격이 일치해야 연결됩니다. 같은 조건 주문이 여러 개라면 직접 확인해야 합니다. UNKNOWN 기록을 자동 삭제하거나 미접수로 단정해 재전송하는 기능은 없습니다.

## 명령행 실행

```powershell
Set-Location "$env:USERPROFILE\projects\invest"
$env:KIS_DEMO_CANO = '본인의모의계좌8자리'
$env:KIS_DEMO_PRODUCT = '01'
python trader.py --domino-home 'D:\실제데이터폴더' run strategy.example.json
python trader.py --domino-home 'D:\실제데이터폴더' run strategy.example.json --execute-demo
python trader.py --domino-home 'D:\실제데이터폴더' sync
```

`run`은 한 번 평가하고 종료합니다. `--execute-demo`가 있어야 주문합니다. `sync`는 저장된 주문일·주문번호로 체결을 조회합니다. 부분체결·전체체결·취소를 저장하고 조회내역이 없으면 NOT_FOUND로 표시하며 기존 상태는 보존합니다. 자동 취소는 하지 않습니다.

대시보드를 직접 실행할 때는:

```powershell
python dashboard.py --domino-home 'D:\실제데이터폴더' --state runtime/orders.sqlite3
```

모의계좌 환경변수가 없어도 페이지는 열리며 설정 상태를 보여줍니다. 서버는 127.0.0.1에만 바인딩하고 Host·Origin·CSRF를 검사합니다. 서버를 외부에 노출하지 마세요. 로컬의 신뢰할 수 없는 프로그램에 대한 사용자 인증 체계는 아닙니다.

## 검증과 참고

```text
python -m unittest discover -s tests -v
```

15개 테스트로 토큰 읽기 전용·교체·만료·binding, 실전/인증 경로 차단, 주문금액 상한, 미리보기, 재시작 중복 방지, 타임아웃 UNKNOWN, 부분/전체체결, 종목 검증, 연속조회, HTTP 상태, CSRF/외부 출처 차단, 전략 저장·실행 중 편집 차단, 재시작 중지를 검증했습니다.

실제 한투 인증·모의 주문·체결, Windows PowerShell 실행과 실제 브라우저 화면 검증은 수행하지 못했습니다. 테스트 통과는 실제 주문 성공이나 전략 수익성을 의미하지 않습니다.

참고 코드:

- `kinggall/domino` 커밋 `33c9b55a934f78c4920e09b8dff3d50d70392df3`: `domino_kis.py`, `docs/KIS.md`.
- `koreainvestment/open-trading-api` 커밋 `4a51f9126e719c352072b14b7df9222cb2691720`: `examples_llm/domestic_stock/order_cash/order_cash.py`, `inquire_daily_ccld/inquire_daily_ccld.py`.
- 모의 TR: 매수 `VTTC0012U`, 매도 `VTTC0011U`, 최근 체결조회 `VTTC0081R`.

### 주문 접수 상태가 불명확할 때

`REQUEST_UNCERTAIN`은 주문 성공·실패를 확정할 수 없는 상태입니다. 자동 실행을 중지한 채 한투 모의 주문내역에서 실제 접수 여부를 먼저 확인하세요. 전략 ID 변경이나 DB 삭제로 재주문하지 마세요. 기존 UNKNOWN은 업데이트 후에도 유지됩니다.

진단은 주문·토큰 발급 없이 모의 시세와 모의 주문내역 조회만 수행합니다:

```powershell
Set-Location "$env:USERPROFILE\projects\invest"
git pull --ff-only
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-dashboard.ps1 -Diagnose
```

진단 출력에는 단계, HTTP 상태, 검증된 한투 오류 코드만 포함합니다. 키·토큰·계좌번호·서버 원문 메시지는 출력하지 않습니다. HTTP 500을 포함한 주문 오류도 자동 재시도하지 않습니다. 기존 오류의 상세 원인은 소급해 복원할 수 없으며, 조회 진단이 주문 권한·주문시간·주문 필드의 유효성까지 보장하지는 않습니다. 기록일이 주말·휴장일이면 시세 조회가 성공해도 주문은 제한될 수 있습니다.

### 저장한 모의계좌 설정 변경

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\projects\invest\start-dashboard.ps1" -Reconfigure
```

이 옵션은 저장된 설정을 다시 입력받고 교체합니다. 데이터 폴더는 명시한 `-DominoHome`이나 기존 `DOMINO_HOME`이 없으면 입력받습니다. 계좌번호와 상품코드는 다시 입력받습니다. 일반 실행 및 `-Diagnose`는 저장된 설정을 재사용합니다. 명시한 계좌 환경변수는 저장된 값보다 우선하며 정상 검증 후 로컬 설정에 반영됩니다. 잘못된 입력은 기존 설정을 덮어쓰지 않습니다. 로컬 파일의 계좌번호는 평문이므로 다른 사람에게 공유하지 마세요. 기존 주문 DB와 토큰은 변경하지 않습니다.

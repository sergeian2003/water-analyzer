# Smart Water Analyzer — 사용자 매뉴얼 및 기술 가이드

Raspberry Pi / Linux, Modbus RTU (RS485) 인터페이스, FastAPI 웹 프레임워크, pywebview 그래픽 셸 및 TailwindCSS 스타일을 기반으로 하는 수질 모니터링 및 산업 자동화를 위한 종합 시스템입니다.

이 시스템은 산업용 수질 센서로부터 24시간 실시간 데이터를 수집하고, 자동 교정, 측정 로그 기록, 외부 액추에이터(릴레이, 4-20mA 아날로그 출력) 제어 및 센서 자동 세정 기능을 제공하도록 설계되었습니다. **가장 최신 업데이트를 통해 8채널 아날로그 입력(AI 4-20mA) 기능이 새롭게 추가되었으며, Linux 환경에서의 RS-485 통신 안정성이 극대화되었습니다.**

---

## 목차

1. 시스템 주요 업데이트 사항 (최신 릴리즈)
2. 프로젝트 구조
3. 하드웨어 요구 사항
4. Raspberry Pi 배포 및 설치
5. 바탕화면 바로가기 생성 (빠른 실행)
6. 비밀번호 및 보안 설정
7. 시스템 구성 요소 실행
8. Kiosk UI 인터페이스 가이드
9. 장비 연결 (RS485 / Modbus RTU)
10. 센서 ID 설정 유틸리티 사용법
11. 문제 해결 (Troubleshooting)

---

## 1. 시스템 주요 업데이트 사항 (최신 릴리즈)

본 버전에서는 산업 현장의 안정성을 위해 다음 5가지 핵심 기술이 새롭게 적용 및 개선되었습니다.

* **Linux RS-485 드라이버 통신 최적화 (DI 통신 오류 해결):** `io_worker` 및 `modbus_worker` 스레드 내에서 동적으로 변경되던 타임아웃 로직을 완전히 제거했습니다. 시스템은 초기화 시 지정된 단일 타임아웃(0.7초)만을 엄격하게 준수합니다. 이를 통해 Linux `usb-serial` 드라이버 특유의 버퍼 초기화(Flush) 문제를 방지했습니다. 또한 통신 버스를 마비시키던 DI 보드의 비트 단위(Fallback) 개별 스캔 로직을 삭제하여 통신 속도를 극대화했습니다.


* **AI (Analog Inputs 4-20mA) 기능 개발 및 통합:** 백엔드에 8채널 `ai_states`가 추가되었으며, `io_worker`에 Modbus 기능 코드 04(FC04)를 이용한 순차적 폴링 시스템이 도입되었습니다. UI의 'Control' 탭에 독립적인 AI 모니터링 카드가 신설되었으며, 통신이 정상적일 때는 파란색 글씨(mA), 단선이나 통신 오류 발생 시 빨간색 'Err' 텍스트로 즉각 변환되는 동적 렌더링이 적용되었습니다. 한국어 및 영어 다국어(ai_title)도 완벽하게 지원합니다.


* **모듈 스캔 (AUTO DETECT) 알고리즘 전면 개편:** 주소 충돌을 막지 못하던 기존 `ignore_id` 스캔 방식을 폐기했습니다. 대신 강력한 하드웨어 기능 식별법을 도입했습니다. 스캐너가 특정 ID에 접근할 때, 먼저 FC04(아날로그 읽기)를 테스트하여 응답하면 AO 모듈로 확실히 분류하고 건너뛰며, FC01(릴레이 상태 읽기)에 응답할 때만 릴레이로 확정합니다. 이 과정은 `try...finally` 블록으로 보호되어 스캔이 끝나면 반드시 백그라운드 워커들을 위해 원래의 타임아웃으로 복구됩니다.


* **UI/UX 토글 스위치(Toggle Switches) 반응형 개선:** 화면 크기나 시스템 폰트 스케일링이 달라질 때 활성화된 스위치(Checkbox)의 흰색 원이 배경을 벗어나던 시각적 버그를 수정했습니다. 딱딱하게 고정되어 있던 픽셀 단위 마진(예: `translate-x-[20px]`)을 TailwindCSS의 반응형 상대 클래스(예: `translate-x-full`, `translate-x-5`)로 전면 교체하여 어떤 환경에서도 완벽한 비율로 압축되도록 수정했습니다.
* **I/O 모듈 ID 설정 초기화 (Zeroing) 버그 픽스:** 설정 모달(팝업창)이 닫혀 있을 때 화면의 빈 값을 읽어와 설정 파일(`config.json`)의 ID를 0으로 날려버리던 치명적 결함을 고쳤습니다. 이제 릴레이(`relay_id`) 및 아날로그 모듈(`ao_id`)의 주소 저장은 `triggerSave()` 함수에서 완전히 분리되었으며, 오직 I/O SETUP 창에서 **"APPLY ID" 버튼(`applyIoIds()`)을 직접 클릭할 때만 저장**됩니다.



---

## 2. 프로젝트 구조

소스 코드는 모듈 단위로 구성되어 있습니다:

```text
water-analyzer/
├── src/
│   ├── main.py             # 메인 애플리케이션 서버, Modbus 로직, API 및 Kiosk UI [최신 통합본]
│   ├── config.json         # 시스템 동적 설정 파일 (자동 생성됨)
│   ├── logs/               # 로컬 CSV 로그 파일 디렉토리 (자동 생성됨)
│   └── static/             # 프론트엔드 정적 파일 (Tailwind, Chart.js 등)
├── tools/
│   ├── sensor_tool.py      # 그래픽(GUI/CustomTkinter) Modbus ID 스캐너 및 설정 툴
│   └── config.py           # RS485 버스의 Slave ID 변경을 위한 콘솔(CLI) 유틸리티
├── requirements.txt        # Python 종속성(라이브러리) 목록
└── README.md               # 현재 열람 중인 사용자 매뉴얼

```

---

## 3. 하드웨어 요구 사항

* **컨트롤러 (Host):** Raspberry Pi 3B+ / 4B / Compute Module 4 또는 Linux OS(Debian/Ubuntu/Raspberry Pi OS Desktop) 기반의 산업용 PC.


* **화면:** 1024x600 px 이상의 해상도를 지원하는 터치스크린 디스플레이.


* **통신 인터페이스:** USB to RS485 컨버터(FTDI / CH340 / CP2102) 또는 SPI to UART / RS485 HAT 확장 보드(Waveshare RS485 HAT 등).


* **지원 센서 및 모듈:**
* 부유물질(MLSS/SS), 흡광도(UV254), 용존산소(DO), ORP, 수중 유분(Oil), pH, 전기전도도(EC), 탁도(Turbidity) 센서.


* **I/O 보드:** 릴레이 및 4-20mA 아날로그 입/출력 확장 모듈 (KM6063 / KM6023 / 8AI-4AO).





---

## 4. Raspberry Pi 배포 및 설치

**1단계. Linux 시스템 종속성 설치**
Linux에서 하드웨어 가속과 함께 pywebview를 실행하려면 WebKit2GTK 라이브러리 및 Python 시스템 구성 요소가 필요합니다. 터미널에서 다음을 실행하세요:

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv python3-dev \
                    libgtk-3-dev libwebkit2gtk-4.0-dev \
                    gobject-introspection libgirepository1.0-dev

```

**2단계. 프로젝트 클론 및 가상 환경 생성**

```bash
cd ~
git clone https://github.com/sergeian2003/water-analyzer.git
cd water-analyzer
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

```

**3단계. 직렬 포트 접근 권한 설정**
현재 사용자가 root 권한 없이 포트에 접근할 수 있도록 dialout 그룹에 추가합니다:

```bash
sudo usermod -a -G dialout $USER

```

**중요:** 이 명령을 실행한 후에는 시스템을 재부팅(`sudo reboot`)해야 권한이 적용됩니다.

---

## 5. 바탕화면 바로가기 생성 (빠른 실행)

산업 현장에서 장비가 재부팅되었거나 프로그램을 다시 켜야 할 때, 매번 터미널을 열고 긴 명령어를 칠 필요가 없도록 바탕화면 아이콘을 생성하는 방법입니다.

Raspberry Pi의 터미널(Terminal)을 열고, 아래의 `cat` 명령어를 전체 복사하여 붙여넣기 한 뒤 Enter를 누르세요. 이 명령어는 바탕화면에 `.desktop` 확장자를 가진 애플리케이션 실행 단축 아이콘을 즉시 생성합니다.

```bash
cat <<EOF > ~/Desktop/WaterAnalyzer.desktop
[Desktop Entry]
Name=Water Analyzer Pro
Comment=Start Smart Water Analyzer
Exec=bash -c "cd ~/water-analyzer && source venv/bin/activate && python src/main.py"
Icon=utilities-terminal
Terminal=false
Type=Application
Categories=Utility;
EOF

```

아이콘이 생성되었다면, 해당 아이콘이 더블클릭으로 바로 실행될 수 있도록 아래 명령어로 실행 권한을 부여합니다.

```bash
chmod +x ~/Desktop/WaterAnalyzer.desktop

```

이제 바탕화면에 생긴 **Water Analyzer Pro** 아이콘을 더블클릭하면, 백그라운드에서 가상환경(venv)이 활성화되며 즉각적으로 Kiosk UI가 전체화면으로 실행됩니다.

---

## 6. 비밀번호 및 보안 설정

* **Administrator (관리자):** 엔지니어링 설정, HMI 교정, Modbus ID 파라미터 변경용. 기본 비밀번호: `1234`. 상단 메뉴의 Setup (설정) 버튼에서 사용.


* **Manufacturer (제조사):** 센서 오염도(Contamination) 카운터 초기화용. 기본 비밀번호: `mfg123`. (관리자 비밀번호는 Setup 탭의 "CHANGE PWD" 버튼을 통해 변경할 수 있으며, `config.json`에 안전하게 암호화되어 저장됩니다.)



---

## 7. 시스템 구성 요소 실행

가상 환경이 활성화된 상태에서 터미널을 통해 수동으로 실행하는 방법입니다:

```bash
cd ~/water-analyzer
source venv/bin/activate

# 메인 Kiosk 인터페이스 실행
python src/main.py

# 센서 주소 설정을 위한 그래픽 유틸리티 실행
python tools/sensor_tool.py

```

---

## 8. Kiosk UI 인터페이스 가이드

**1. 감시화면 (Monitoring)**
메인 화면입니다. 센서의 현재 값, 측정 단위, 오염도(Contamination %) 및 연결 상태를 실시간으로 표시합니다.

**2. 트렌드 (Trends)**
실시간 측정값의 변화를 시각적으로 보여주는 그래프 탭입니다. 모든 센서를 한 번에 보거나 특정 센서만 선택하여 집중 분석할 수 있습니다.

**3. 자료조회 (Logs / Data)**
측정값(5분 데이터, 1시간 평균) 및 알람 이력을 확인하고 필터링합니다. SHOW GRAPH 버튼으로 그래프를 확인하거나 EXPORT TO FILE 버튼을 통해 USB로 CSV 데이터를 추출할 수 있습니다.

**4. 제어 (Control)**
시스템에 연결된 외부 I/O 모듈의 실시간 입출력 상태를 확인하고 조작합니다.

* **DI / MANUAL RELAY OVERRIDE:** 디지털 입력 상태 모니터링 및 물리적 릴레이 스위치 수동 제어.


* **AI (ANALOG INPUTS):** 8채널 아날로그 입력의 실시간 4-20mA 전류값을 표시합니다. 센서가 정상적으로 전류를 보내면 파란색 텍스트로 값이 표출되며, 통신 오류 시 빨간색 텍스트로 **Err**가 표시되어 직관적인 장애 파악이 가능합니다.
* **MANUAL ANALOG OUTPUT:** PLC 테스트를 위한 4-20mA 아날로그 출력 수동 발생기.



**5. 설정 (Setup)**
시스템 관리자를 위한 엔지니어링 탭입니다.

* **교정 (CAL):** HMI 소프트웨어 오프셋 설정 및 센서 하드웨어(EEPROM) 영점/스팬 교정.


* **I/O SETUP (APPLY ID):** 릴레이 보드와 아날로그 보드의 통신 ID를 지정합니다. 하단의 **APPLY ID** 버튼을 눌러야만 시스템 설정에 영구적으로 반영됩니다.
* **AUTO DETECT:** I/O 모듈의 주소를 자동으로 스캔합니다. 이번 업데이트로 기능 코드(FC04 및 FC01)를 철저히 검증하여 아날로그 보드와 릴레이 보드를 한 치의 오차 없이 자동 구분합니다.

---

## 9. 장비 연결 (RS485 / Modbus RTU)

RS485 버스는 '데이지 체인 (Daisy Chain)' 토폴로지로 꼬임선(Twisted Pair)을 활용하여 배선해야 합니다. 센서 전원과 통신 모듈은 반드시 공통 접지(GND)를 공유해야 디지털 입력(NPN) 및 통신이 노이즈 없이 정상적으로 작동합니다.

```text
[Raspberry Pi / USB-RS485]
       |
       +=== A (+) ===================== A (+) Sensor 1 ==== A (+) Sensor 2 ...
       +=== B (-) ===================== B (-) Sensor 1 ==== B (-) Sensor 2 ...
       +=== GND (Shield) ============== GND Sensor 1 ====== GND Sensor 2 ...

```

---

## 10. 센서 ID 설정 유틸리티 사용법

**주의 사항:** RS485 버스에서 Modbus ID를 변경할 때는 주소 충돌을 막기 위해 **반드시 하나의 장치만 물리적으로 연결되어 있어야** 합니다!

1. 실행 중인 Kiosk 메인 애플리케이션(main.py)을 종료합니다.


2. 설정할 센서 단 1개만 연결한 후 `python tools/sensor_tool.py`를 실행합니다.


3. **DETECT**를 눌러 스캔하거나 Current ID에 255 (브로드캐스트)를 입력합니다.


4. NEW ID에 새 주소를 입력하고 CHANGE ID를 누른 뒤 센서를 재부팅(전원 차단 후 재인가)합니다.



---

## 11. 문제 해결 (Troubleshooting)

**Q. 장치 ID가 재시작 시 0으로 초기화됩니다.**

* 과거 모달 닫힘 상태에서 자동 저장되던 버그로, **현재 완벽하게 해결되었습니다.** 설정 탭의 `I/O SETUP` 창에서 설정값을 변경한 후 반드시 `APPLY ID` 버튼을 눌러 저장하시기 바랍니다.

**Q. 센서 값들이 모두 "ERROR"로 표시되고 작동하지 않습니다.**

* 물리적으로 선이 연결되어 있지 않은 센서를 시스템(SETUP)에서 켜두면, 시스템이 해당 센서를 찾기 위해 계속해서 신호를 대기(Timeout)하며 병목 현상이 생깁니다. 물리적으로 결선되지 않은 센서는 반드시 SETUP 화면에서 비활성화(OFF) 처리해 주십시오.

**Q. 제어(Control) 탭의 AI 블록이 모두 빨간색 "Err"로 표시됩니다.**

* 아날로그 입력 모듈과의 통신 실패를 의미합니다. `Setup > I/O SETUP` 메뉴에서 **ANALOG OUTPUT ID**가 실제 설치된 모듈의 하드웨어 딥스위치 주소와 일치하는지 확인하고, `APPLY ID`를 다시 한 번 눌러주십시오.

**Q. /dev/ttyUSB0 접근 거부 (Permission Denied) 오류가 뜹니다.**

* Linux에서 포트 권한이 부족할 때 발생합니다. 터미널을 열고 `sudo usermod -a -G dialout $USER` 명령어를 실행한 후 시스템을 반드시 **재부팅**하세요.

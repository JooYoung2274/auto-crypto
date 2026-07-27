# 배포 가이드 (판매자용)

Windows PC 없이 `.exe`를 만드는 방법과, macOS 서명·공증 설정 절차입니다.

## 빌드 실행

1. GitHub 저장소 → **Actions** 탭
2. 왼쪽에서 **데스크탑 앱 빌드** 선택
3. 오른쪽 **Run workflow** → 브랜치 `main` → **Run workflow**
4. 10~15분 후 실행 페이지 하단 **Artifacts** 에서 다운로드
   - `CoinAgentsOffice-windows.zip`
   - `CoinAgentsOffice-macos.zip`

버전 태그를 밀어도 자동 실행됩니다:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

PyInstaller는 크로스 컴파일이 되지 않으므로 Windows 산출물은 반드시
Windows 러너에서 나옵니다. 이 워크플로가 그 러너를 대신합니다.

빌드 전에 백엔드·프론트엔드 테스트가 먼저 돌고, **하나라도 실패하면
패키징하지 않습니다.** 깨진 엔진이 구매자에게 나가지 않도록 하는 게이트입니다.

---

## macOS 서명 · 공증 (Apple 개발자 계정 필요)

설정하면 구매자가 경고 없이 바로 실행합니다. Secrets가 없으면 워크플로는
서명 단계를 건너뛰고 빌드만 하므로, 나중에 추가해도 됩니다.

### 1. 인증서 준비

Xcode 또는 [Apple Developer](https://developer.apple.com/account/resources/certificates)에서
**Developer ID Application** 인증서를 만들고, 키체인에서 `.p12`로 내보냅니다
(내보낼 때 암호를 지정).

```bash
# .p12 를 base64 한 줄로 변환 (클립보드에 복사)
base64 -i Certificates.p12 | pbcopy

# 서명 아이디 확인 — "Developer ID Application: 이름 (TEAMID)" 전체를 그대로 사용
security find-identity -v -p codesigning
```

### 2. 앱 암호 발급

[appleid.apple.com](https://appleid.apple.com) → 로그인 및 보안 → **앱 암호**
에서 생성합니다. **Apple ID 계정 암호가 아닙니다.**

### 3. GitHub Secrets 등록

저장소 → Settings → Secrets and variables → **Actions** → New repository secret

| 이름 | 값 |
|---|---|
| `MACOS_CERT_P12` | 1단계에서 만든 base64 문자열 |
| `MACOS_CERT_PASSWORD` | `.p12` 내보낼 때 지정한 암호 |
| `MACOS_SIGN_IDENTITY` | `Developer ID Application: 이름 (TEAMID)` |
| `APPLE_ID` | Apple 개발자 계정 이메일 |
| `APPLE_TEAM_ID` | 10자리 팀 ID |
| `APPLE_APP_PASSWORD` | 2단계에서 만든 앱 암호 |

여섯 개가 모두 있어야 공증까지 진행됩니다. `MACOS_CERT_P12`만 있으면 서명만
하고 공증은 건너뜁니다 (이 경우 Gatekeeper 경고가 남습니다).

### 확인

```bash
spctl -a -vvv -t install /Applications/CoinAgentsOffice.app
# "accepted / source=Notarized Developer ID" 가 나와야 정상
```

---

## Windows는 서명 없이 배포

코드 서명 인증서(연 20~40만원)가 없으면 구매자에게 **"Windows의 PC 보호"**
SmartScreen 경고가 뜹니다. 바이러스 판정이 아니라 서명이 없는 프로그램에
대한 기본 안내이며, `[추가 정보] → [실행]`으로 통과합니다.

압축 안에 `사용안내.txt`가 함께 들어가 이 절차를 설명합니다. 판매 페이지
상단에도 같은 내용을 적어두면 문의가 크게 줄어듭니다.

> 참고: EV 코드 서명 인증서를 쓰면 경고가 즉시 사라지고, 일반(OV) 인증서는
> 평판이 쌓일 때까지 경고가 남을 수 있습니다.

### WebView2 런타임

Windows 빌드는 화면 표시에 Microsoft Edge WebView2 런타임을 씁니다.
Windows 11과 최신 Windows 10에는 기본 탑재돼 있지만, 없는 PC에서는 창이
비어 보입니다. 안내문에 [Evergreen 부트스트래퍼](https://developer.microsoft.com/microsoft-edge/webview2/)
링크를 넣어두었습니다.

---

## 배포 전 점검

- [ ] 워크포워드 검증(`📊 수익성 검증`)을 돌려 번들 챔피언이 여전히 쓸 만한지 확인
- [ ] 신규 설치 시나리오 확인 — 앱데이터 폴더를 지우고 실행해 온보딩과 기본 챔피언이 뜨는지
- [ ] 판매 페이지에 면책 문구 게시 (수익률·승률 수치를 성과 보장처럼 쓰지 않기)
- [ ] 이용약관 / 환불 정책 명시
- [ ] 저장소를 private으로 전환하거나 판매판을 분리

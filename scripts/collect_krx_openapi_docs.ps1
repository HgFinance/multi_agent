param(
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$workspaceRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $workspaceRoot "docs\06-integrations\krx-openapi"
}

$baseUrl = "https://openapi.krx.co.kr"
$dataDomain = "https://data-dbg.krx.co.kr"
$listPath = "/contents/OPP/USES/service/OPPUSES001_S1D1.cmd"
$collectedAt = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTimeOffset]::UtcNow,
    "Korea Standard Time"
).ToString("yyyy-MM-dd HH:mm:ss zzz")

$groups = @(
    [ordered]@{ order = 1; path = "idx"; name = "지수"; screenId = "OPPUSES001"; file = "01-index.md" },
    [ordered]@{ order = 2; path = "sto"; name = "주식"; screenId = "OPPUSES002"; file = "02-stock.md" },
    [ordered]@{ order = 3; path = "etp"; name = "증권상품"; screenId = "OPPUSES003"; file = "03-securities-products.md" },
    [ordered]@{ order = 4; path = "bon"; name = "채권"; screenId = "OPPUSES004"; file = "04-bond.md" },
    [ordered]@{ order = 5; path = "drv"; name = "파생상품"; screenId = "OPPUSES005"; file = "05-derivatives.md" },
    [ordered]@{ order = 6; path = "gen"; name = "일반상품"; screenId = "OPPUSES006"; file = "06-general-commodities.md" },
    [ordered]@{ order = 7; path = "esg"; name = "ESG"; screenId = "OPPUSES007"; file = "07-esg.md" }
)

Add-Type -AssemblyName System.Net.Http
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseCookies = $true
$handler.CookieContainer = New-Object System.Net.CookieContainer
$handler.AutomaticDecompression = [System.Net.DecompressionMethods]::GZip -bor [System.Net.DecompressionMethods]::Deflate
$httpClient = New-Object System.Net.Http.HttpClient($handler)
$httpClient.Timeout = [TimeSpan]::FromSeconds(30)
$httpClient.DefaultRequestHeaders.UserAgent.ParseAdd("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
$httpClient.DefaultRequestHeaders.Accept.ParseAdd("text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
$httpClient.DefaultRequestHeaders.AcceptLanguage.ParseAdd("ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-KrxRequest {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$RequestFactory,
        [int]$MaxAttempts = 4
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $request = & $RequestFactory
        $response = $null
        try {
            $response = $httpClient.SendAsync($request).GetAwaiter().GetResult()
            $response.EnsureSuccessStatusCode() | Out-Null
            return $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }
        catch {
            if ($attempt -eq $MaxAttempts) {
                throw "KRX Open API 문서 호출 실패: $($request.RequestUri) ($($_.Exception.Message))"
            }
            Start-Sleep -Milliseconds (300 * [Math]::Pow(2, $attempt - 1))
        }
        finally {
            if ($null -ne $response) { $response.Dispose() }
            if ($null -ne $request) { $request.Dispose() }
        }
    }
}

function Invoke-KrxHtml {
    param([Parameter(Mandatory = $true)][string]$Path)

    $url = if ($Path.StartsWith("http")) { $Path } else { "$baseUrl$Path" }
    return Invoke-KrxRequest -RequestFactory {
        New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $url)
    }
}

function Invoke-KrxList {
    param([Parameter(Mandatory = $true)][string]$CategoryPath)

    $encodedPath = [Uri]::EscapeDataString($CategoryPath)
    return (Invoke-KrxRequest -RequestFactory {
        $request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, "$baseUrl$listPath")
        $request.Headers.Add("X-Requested-With", "XMLHttpRequest")
        $request.Headers.Add("Origin", $baseUrl)
        $request.Headers.Referrer = [Uri]"$baseUrl/contents/OPP/USES/service/OPPUSES001_S1.cmd"
        $request.Headers.Accept.Clear()
        $request.Headers.Accept.ParseAdd("application/json, text/javascript, */*; q=0.01")
        $request.Content = New-Object System.Net.Http.StringContent("path=$encodedPath", [Text.Encoding]::UTF8, "application/x-www-form-urlencoded")
        return $request
    }) | ConvertFrom-Json
}

function ConvertTo-PlainText {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return "" }
    $text = [Net.WebUtility]::HtmlDecode([string]$Value)
    $text = $text -replace "(?i)<br\s*/?>", " "
    $text = $text -replace "<[^>]+>", " "
    $text = $text -replace "[\r\n\t]+", " "
    $text = $text -replace "\s{2,}", " "
    return $text.Trim()
}

function ConvertTo-MarkdownCell {
    param([AllowNull()][object]$Value)

    $text = ConvertTo-PlainText $Value
    $text = $text.Replace("\", "\\")
    $text = $text.Replace("|", "\|")
    $text = $text.Replace('`', '\`')
    if ([string]::IsNullOrWhiteSpace($text)) { return "-" }
    return $text
}

function ConvertTo-LinkLabel {
    param([AllowNull()][object]$Value)

    return (ConvertTo-MarkdownCell $Value).Replace("[", "\[").Replace("]", "\]")
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $content = [string]::Join([Environment]::NewLine, $Lines)
    $content = $content.TrimEnd([char[]]"`r`n") + [Environment]::NewLine
    [IO.File]::WriteAllText($Path, $content, $utf8NoBom)
}

function Get-AttributeValue {
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlElement]$Node,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $value = $Node.GetAttribute($Name)
    if ($null -eq $value) { return "" }
    return $value
}

function Get-SpecFields {
    param(
        [Parameter(Mandatory = $true)][xml]$Xml,
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][System.Xml.XmlNamespaceManager]$NamespaceManager
    )

    $rows = @()
    $blocks = @($Xml.SelectNodes("/b:transaction/b:$Section/b:block", $NamespaceManager))
    foreach ($block in $blocks) {
        $blockName = $block.GetAttribute("name")
        $repeat = $block.GetAttribute("repeat")
        foreach ($field in @($block.SelectNodes("b:field", $NamespaceManager))) {
            $rows += [pscustomobject]@{
                block = $blockName
                repeat = $repeat
                name = Get-AttributeValue -Node $field -Name "name"
                label = Get-AttributeValue -Node $field -Name "label"
                type = Get-AttributeValue -Node $field -Name "type"
                size = Get-AttributeValue -Node $field -Name "size"
                format = Get-AttributeValue -Node $field -Name "format"
                default = Get-AttributeValue -Node $field -Name "default"
                sample = ConvertTo-PlainText $field.InnerText
            }
        }
    }
    return $rows
}

function Get-ApiSpec {
    param(
        [Parameter(Mandatory = $true)][object]$Api,
        [Parameter(Mandatory = $true)][object]$Group
    )

    $detailPath = "/contents/OPP/USES/service/$($Group.screenId)_S2.cmd?BO_ID=$($Api.BO_ID)"
    $html = Invoke-KrxHtml $detailPath
    $bldMatch = [regex]::Match($html, "(?s)var\s+bld\s*=\s*'([^']*)'")
    if (-not $bldMatch.Success -or [string]::IsNullOrWhiteSpace($bldMatch.Groups[1].Value)) {
        throw "KRX 개발 명세를 찾지 못함: $detailPath"
    }

    $xmlText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($bldMatch.Groups[1].Value))
    [xml]$xml = $xmlText
    $namespaceManager = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $namespaceManager.AddNamespace("b", "http://www.cyber-i.com/xml/bld")
    $inputFields = @(Get-SpecFields -Xml $xml -Section "input" -NamespaceManager $namespaceManager)
    $outputFields = @(Get-SpecFields -Xml $xml -Section "output" -NamespaceManager $namespaceManager)

    $sampleUrlMatch = [regex]::Match($html, 'name="apiUrl"\s+value="([^"]+)"')
    $sampleUrl = if ($sampleUrlMatch.Success) { [Net.WebUtility]::HtmlDecode($sampleUrlMatch.Groups[1].Value) } else { "$dataDomain/svc/sample/$($Api.BO_PATH)" }
    $approvedUrl = $sampleUrl.Replace("/svc/sample/apis/", "/svc/apis/")

    return [pscustomobject]@{
        detailPath = $detailPath
        detailUrl = "$baseUrl$detailPath"
        sampleUrl = $sampleUrl
        approvedUrl = $approvedUrl
        inputFields = $inputFields
        outputFields = $outputFields
        name = ConvertTo-PlainText $xml.transaction.info.name
        description = ConvertTo-PlainText $xml.transaction.info.description
        version = ConvertTo-PlainText $xml.transaction.info.version
    }
}

function Add-ApiDetail {
    param(
        [Parameter(Mandatory = $true)][System.Collections.ArrayList]$Lines,
        [Parameter(Mandatory = $true)][object]$Api,
        [Parameter(Mandatory = $true)][object]$Group,
        [Parameter(Mandatory = $true)][object]$Spec,
        [Parameter(Mandatory = $true)][int]$Number
    )

    [void]$Lines.Add("<a id=""api-$($Api.WRTRPT_ENG_NM)""></a>")
    [void]$Lines.Add("")
    [void]$Lines.Add("## $Number. $(ConvertTo-PlainText $Api.WRTRPT_KOR_NM)")
    [void]$Lines.Add("")
    [void]$Lines.Add("| 항목 | 값 |")
    [void]$Lines.Add("|---|---|")
    [void]$Lines.Add("| API ID | ``$(ConvertTo-MarkdownCell $Api.WRTRPT_ENG_NM)`` |")
    [void]$Lines.Add("| 내부 명세 ID | ``$(ConvertTo-MarkdownCell $Api.BO_ID)`` |")
    [void]$Lines.Add("| 명세 버전 | ``$(ConvertTo-MarkdownCell $Spec.version)`` |")
    [void]$Lines.Add("| 등록일 | $(ConvertTo-MarkdownCell $Api.FST_WRTR_DDTM) |")
    [void]$Lines.Add("| 최근 수정일 | $(ConvertTo-MarkdownCell $Api.LST_WRTR_DDTM) |")
    [void]$Lines.Add("| 설명 | $(ConvertTo-MarkdownCell $Api.DSC) |")
    [void]$Lines.Add("| 승인 API URL | ``$(ConvertTo-MarkdownCell $Spec.approvedUrl)`` |")
    [void]$Lines.Add("| 샘플 API URL | ``$(ConvertTo-MarkdownCell $Spec.sampleUrl)`` |")
    [void]$Lines.Add("| 응답 형식 | 기본 JSON, ``.json`` JSON, ``.xml`` XML |")
    [void]$Lines.Add("| 인증 | HTTP 요청 헤더 ``AUTH_KEY`` |")
    [void]$Lines.Add("| 공식 상세 | [KRX 원문]($($Spec.detailUrl)) |")
    [void]$Lines.Add("")

    [void]$Lines.Add("### 요청 인자")
    [void]$Lines.Add("")
    if ($Spec.inputFields.Count -eq 0) {
        [void]$Lines.Add("추가 Query 인자가 없습니다.")
        [void]$Lines.Add("")
    }
    else {
        [void]$Lines.Add("| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |")
        [void]$Lines.Add("|---|---|---|---|---:|---|")
        foreach ($field in $Spec.inputFields) {
            [void]$Lines.Add("| $(ConvertTo-MarkdownCell $field.block) | ``$(ConvertTo-MarkdownCell $field.name)`` | $(ConvertTo-MarkdownCell $field.label) | $(ConvertTo-MarkdownCell $field.type) | $(ConvertTo-MarkdownCell $field.size) | ``$(ConvertTo-MarkdownCell $field.sample)`` |")
        }
        [void]$Lines.Add("")
    }

    [void]$Lines.Add("### 응답 필드")
    [void]$Lines.Add("")
    [void]$Lines.Add("| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |")
    [void]$Lines.Add("|---|---|---|---|---|---|")
    foreach ($field in $Spec.outputFields) {
        [void]$Lines.Add("| $(ConvertTo-MarkdownCell $field.block) | ``$(ConvertTo-MarkdownCell $field.name)`` | $(ConvertTo-MarkdownCell $field.label) | $(ConvertTo-MarkdownCell $field.type) | ``$(ConvertTo-MarkdownCell $field.format)`` | ``$(ConvertTo-MarkdownCell $field.default)`` |")
    }
    [void]$Lines.Add("")
}

try {
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
    Write-Host "KRX 문서 세션 초기화"
    [void](Invoke-KrxHtml -Path "/contents/OPP/USES/service/OPPUSES001_S1.cmd")
    $groupResults = @()
    $totalApiCount = 0
    $totalInputFieldCount = 0
    $totalOutputFieldCount = 0

    foreach ($group in $groups) {
        Write-Host ("[{0}/7] {1} 목록과 명세 수집" -f $group.order, $group.name)
        $listResponse = Invoke-KrxList -CategoryPath $group.path
        $apis = @($listResponse.output.result | Sort-Object { [int]$_.WRTRPT_ORD })
        $totalApiCount += $apis.Count

        $lines = New-Object System.Collections.ArrayList
        $listUrl = "$baseUrl/contents/OPP/USES/service/$($group.screenId)_S1.cmd"
        [void]$lines.Add("# KRX Open API $($group.name) 전체 참조")
        [void]$lines.Add("")
        [void]$lines.Add("> [KRX 공식 서비스 화면]($listUrl)과 상세 개발 명세를 $collectedAt 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.")
        [void]$lines.Add("")
        [void]$lines.Add("## API 목록")
        [void]$lines.Add("")
        [void]$lines.Add("API $($apis.Count)개")
        [void]$lines.Add("")
        [void]$lines.Add("| 번호 | API | API ID | 제공 범위 | 최근 수정일 |")
        [void]$lines.Add("|---:|---|---|---|---|")
        $apiNumber = 0
        $specs = @()
        foreach ($api in $apis) {
            $apiNumber++
            Write-Host ("  - {0}/{1} {2}" -f $apiNumber, $apis.Count, $api.WRTRPT_KOR_NM)
            $spec = Get-ApiSpec -Api $api -Group $group
            $specs += [pscustomobject]@{ api = $api; spec = $spec; number = $apiNumber }
            $totalInputFieldCount += $spec.inputFields.Count
            $totalOutputFieldCount += $spec.outputFields.Count
            $label = ConvertTo-LinkLabel $api.WRTRPT_KOR_NM
            [void]$lines.Add("| $apiNumber | [$label](#api-$($api.WRTRPT_ENG_NM)) | ``$(ConvertTo-MarkdownCell $api.WRTRPT_ENG_NM)`` | $(ConvertTo-MarkdownCell $api.DSC) | $(ConvertTo-MarkdownCell $api.LST_WRTR_DDTM) |")
        }
        [void]$lines.Add("")
        [void]$lines.Add("---")
        [void]$lines.Add("")

        foreach ($item in $specs) {
            Add-ApiDetail -Lines $lines -Api $item.api -Group $group -Spec $item.spec -Number $item.number
            [void]$lines.Add("---")
            [void]$lines.Add("")
        }
        Write-Utf8File -Path (Join-Path $OutputRoot $group.file) -Lines $lines.ToArray([string])
        $groupResults += [pscustomobject]@{
            path = $group.path
            name = $group.name
            file = $group.file
            apiCount = $apis.Count
        }
    }

    $index = New-Object System.Collections.ArrayList
    [void]$index.Add("# KRX Data Marketplace Open API 전체 참조")
    [void]$index.Add("")
    [void]$index.Add("> [KRX 공식 서비스 목록]($baseUrl/contents/OPP/INFO/service/OPPINFO004.cmd)과 31개 상세 명세를 $collectedAt 에 수집한 HgFinance 개발용 참조입니다. 최신 API 계약·이용승인·약관은 KRX 원문을 우선합니다.")
    [void]$index.Add("")
    [void]$index.Add("## 수집 결과")
    [void]$index.Add("")
    [void]$index.Add("| 항목 | 결과 |")
    [void]$index.Add("|---|---:|")
    [void]$index.Add("| 서비스 분류 | $($groups.Count)개 |")
    [void]$index.Add("| API | ${totalApiCount}개 |")
    [void]$index.Add("| 요청 인자 필드 | ${totalInputFieldCount}개 |")
    [void]$index.Add("| 응답 필드 | ${totalOutputFieldCount}개 |")
    [void]$index.Add("")
    [void]$index.Add("## 전체 API 지도")
    [void]$index.Add("")
    [void]$index.Add("| 경로 | 분류 | API 수 | 상세 문서 |")
    [void]$index.Add("|---|---|---:|---|")
    foreach ($result in $groupResults) {
        [void]$index.Add("| ``$($result.path)`` | $($result.name) | $($result.apiCount) | [전체 요청·응답 계약]($($result.file)) |")
    }
    [void]$index.Add("")
    [void]$index.Add("## 공통 호출 계약")
    [void]$index.Add("")
    [void]$index.Add("- 방식: ``GET``")
    [void]$index.Add("- 승인 API 기본 경로: ``$dataDomain/svc/apis/{category}/{api_id}``")
    [void]$index.Add("- 샘플 API 기본 경로: ``$dataDomain/svc/sample/apis/{category}/{api_id}``")
    [void]$index.Add("- 인증: HTTP 요청 헤더 ``AUTH_KEY: {issued-key}``")
    [void]$index.Add("- 응답: 접미사가 없으면 JSON, ``.json``은 JSON, ``.xml``은 XML")
    [void]$index.Add("- Query: 각 상세 문서의 요청 인자를 URL Query String으로 전달한다.")
    [void]$index.Add("- JSON 응답: 명세의 Output Block 이름을 최상위 키로 사용하고 행 배열을 반환한다.")
    [void]$index.Add("")
    [void]$index.Add('```http')
    [void]$index.Add("GET /svc/apis/sto/stk_bydd_trd?basDd=20260102 HTTP/1.1")
    [void]$index.Add("Host: data-dbg.krx.co.kr")
    [void]$index.Add("AUTH_KEY: {issued-key}")
    [void]$index.Add('```')
    [void]$index.Add("")
    [void]$index.Add("## 이용 절차")
    [void]$index.Add("")
    [void]$index.Add("1. Data Marketplace 계정을 만들고 인증키를 신청한다.")
    [void]$index.Add("2. 상세 화면의 샘플 기능과 개발 명세로 계약을 확인한다.")
    [void]$index.Add("3. 필요한 API마다 이용 기간과 목적을 지정해 활용 신청한다.")
    [void]$index.Add("4. 관리자 승인 후 발급 키로 승인 API 경로를 호출한다.")
    [void]$index.Add("")
    [void]$index.Add("인증키 발급과 개별 API 활용 승인은 별도 단계다. 문서에 엔드포인트가 공개되어 있어도 승인 전 운영 호출 권한이 생기는 것은 아니다.")
    [void]$index.Add("")
    [void]$index.Add("## 약관·출시 게이트")
    [void]$index.Add("")
    [void]$index.Add("> 아래 내용은 [2025년 12월 26일 시행 국문 약관]($baseUrl/contents/OPP/INFO/OPPINFO002.jsp)의 구현 영향 요약이다. 법률 자문이 아니며, 출시 전 최신 약관과 별도 데이터 계약을 확인한다.")
    [void]$index.Add("")
    [void]$index.Add("| 약관 항목 | 현재 공개 조건 | HgFinance 처리 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| 이용 목적 | 비상업적 목적만 허용 | 유료·상업 서비스에는 그대로 사용하지 않고 별도 상업 이용 계약을 확보한다. |")
    [void]$index.Add("| 제3자 제공 | KRX 제공 정보를 제3자에게 제공할 수 없음 | 사용자 화면·API·다운로드로 원 데이터를 재배포하지 않는다. |")
    [void]$index.Add("| 표시 의무 | 화면에 한국거래소 통계정보 사용 사실 표시 | KRX 파생 화면과 리포트에 Source Attribution을 넣는다. |")
    [void]$index.Add("| 호출 제한 | 키당 일 10,000회 이하 | 일일 예산, 캐시, 중복제거와 차단기를 둔다. |")
    [void]$index.Add("| 키 유효기간 | 발급일부터 1년, 연장 가능 | 만료 30일 전 운영 알림과 갱신 Runbook을 둔다. |")
    [void]$index.Add("| 장기 미사용 | 12개월 미사용 키는 삭제될 수 있음 | 활성 키 상태와 마지막 성공 호출을 감시한다. |")
    [void]$index.Add("| 계약 종료 | 종료 후 제공 정보 이용 불가 | 데이터 사용권 만료와 보존·삭제 정책을 계약에 맞춘다. |")
    [void]$index.Add("")
    [void]$index.Add("따라서 이 공개 API는 연구·내부 검증용 Source로는 유용하지만, 개인 헤지펀드 서비스의 상업적 Production Source로 자동 승인된 것으로 간주하면 안 된다. 상업 이용, 내부 모델 학습, 결과 노출, 파생지표 제공과 재배포 범위를 KRX와 별도로 확인하는 것을 Production Launch Gate로 둔다.")
    [void]$index.Add("")
    [void]$index.Add("## HgFinance 적용 원칙")
    [void]$index.Add("")
    [void]$index.Add("KRX Open API는 LS증권 WebSocket 실시간 Feed를 대체하지 않는다. 거래소 공식 일별 통계, 종목 기본정보, 파생상품·채권·일반상품과 ESG Reference를 보강하는 EOD Data Source다.")
    [void]$index.Add("")
    [void]$index.Add('```text')
    [void]$index.Add("KRX Open API")
    [void]$index.Add("  -> Research Collector")
    [void]$index.Add("  -> Raw JSON/XML Archive")
    [void]$index.Add("  -> Schema Validation / Idempotent Upsert")
    [void]$index.Add("  -> Supabase Metadata + TimescaleDB EOD Series")
    [void]$index.Add("  -> Research API / Quant Dataset")
    [void]$index.Add("  -> Agentic RAG Evidence Metadata")
    [void]$index.Add('```')
    [void]$index.Add("")
    [void]$index.Add("### 사용 우선순위")
    [void]$index.Add("")
    [void]$index.Add("| 우선순위 | 데이터 | 활용 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| P0 | 유가증권·코스닥·코넥스 종목기본정보 | Instrument Master와 ``stock_code`` 검증 |")
    [void]$index.Add("| P0 | 주식·ETF·ETN·ELW 일별매매정보 | LS 수집 데이터 EOD 대사와 백필 |")
    [void]$index.Add("| P1 | 지수·선물·옵션 일별정보 | 벤치마크, 파생 Feature와 전략 검증 |")
    [void]$index.Add("| P1 | 채권·금·석유·배출권 | Cross-asset Regime Feature |")
    [void]$index.Add("| P2 | ESG 지수·증권상품·사회책임투자채권 | ESG Universe와 리서치 메타데이터 |")
    [void]$index.Add("")
    [void]$index.Add("### 수집 주기")
    [void]$index.Add("")
    [void]$index.Add("| 데이터 | 권장 시작 주기 | 방식 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| 종목기본정보 | 거래일 장 마감 후 1회 | 기준일자 단위 전체 Snapshot과 변경분 upsert |")
    [void]$index.Add("| 일별매매정보 | 거래일 장 마감 후 지연을 두고 1회 | 날짜별 증분 수집, 다음 날 누락 재확인 |")
    [void]$index.Add("| 지수·파생·채권·일반상품 | 거래일 장 마감 후 1회 | 시장별 워터마크와 재시도 |")
    [void]$index.Add("| ESG | 거래일 또는 주 1회 | 변경 빈도를 관측한 뒤 주기 조정 |")
    [void]$index.Add("")
    [void]$index.Add("### 저장 모델")
    [void]$index.Add("")
    [void]$index.Add("| 저장소 | 역할 | 대표 키 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| Object Storage | 원본 JSON/XML과 수집 Manifest | ``category/api_id/bas_dd/content_hash`` |")
    [void]$index.Add("| ``research.krx_instruments`` | 주식·ETF·ETN·ELW 종목 기준정보 | ``market + isu_cd + valid_from`` |")
    [void]$index.Add("| TimescaleDB ``market.krx_daily_stats`` | OHLCV·거래대금·시가총액 일별 시계열 | ``bas_dd + market + isu_cd`` |")
    [void]$index.Add("| TimescaleDB ``market.krx_derivative_daily`` | 선물·옵션 일별 시계열 | ``bas_dd + isu_cd`` |")
    [void]$index.Add("| ``research.krx_reference`` | 지수·채권·상품·ESG Reference | ``api_id + natural_key + valid_from`` |")
    [void]$index.Add("| ``research.source_runs`` | 호출 예산, 워터마크, 상태와 오류 | ``source + api_id + run_id`` |")
    [void]$index.Add("")
    [void]$index.Add("### 품질·중복 관리")
    [void]$index.Add("")
    [void]$index.Add("1. ``api_id + 요청 인자``를 정렬해 ``request_hash``를 만든다.")
    [void]$index.Add("2. 원문 ``content_hash``가 같으면 중복 정규화를 건너뛴다.")
    [void]$index.Add("3. 날짜·시장·종목의 자연키로 멱등 upsert하되 이전 원문은 보존한다.")
    [void]$index.Add("4. LS EOD 집계와 KRX 일별 통계를 대사하고 차이는 품질 Finding으로 남긴다.")
    [void]$index.Add("5. 숫자 필드가 문자열로 제공될 수 있으므로 명세의 형식과 단위를 기준으로 Decimal 변환한다.")
    [void]$index.Add("6. 공식 문서에는 통합 오류 코드 표가 없으므로 승인 환경의 HTTP 상태와 오류 Payload를 Fixture로 축적한다.")
    [void]$index.Add("")
    [void]$index.Add("### Agentic RAG 계약")
    [void]$index.Add("")
    [void]$index.Add("- Agent는 KRX를 직접 호출하지 않고 Research API와 Quant Dataset을 사용한다.")
    [void]$index.Add("- 숫자 계산은 구조화 테이블에서 수행하고, RAG에는 Source·기준일·API ID·수집시각을 Evidence로 넣는다.")
    [void]$index.Add("- 약관상 제3자 제공 금지를 고려해 원 응답을 사용자에게 그대로 노출하는 Tool을 만들지 않는다.")
    [void]$index.Add("- 모델 학습·파인튜닝·임베딩 이용 가능 범위는 별도 계약 확인 전 허용하지 않는다.")
    [void]$index.Add("")
    [void]$index.Add("## 재수집")
    [void]$index.Add("")
    [void]$index.Add("저장소 루트에서 다음 명령을 실행한다.")
    [void]$index.Add("")
    [void]$index.Add('```powershell')
    [void]$index.Add(".\scripts\collect_krx_openapi_docs.ps1")
    [void]$index.Add('```')

    Write-Utf8File -Path (Join-Path $OutputRoot "README.md") -Lines $index.ToArray([string])
    Write-Host "완료: $totalApiCount APIs, $totalInputFieldCount input fields, $totalOutputFieldCount output fields"
}
finally {
    if ($null -ne $httpClient) { $httpClient.Dispose() }
}

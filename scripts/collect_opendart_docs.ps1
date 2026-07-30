param(
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$workspaceRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $workspaceRoot "docs\06-integrations\opendart"
}

$baseUrl = "https://opendart.fss.or.kr"
$collectedAt = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTimeOffset]::UtcNow,
    "Korea Standard Time"
).ToString("yyyy-MM-dd HH:mm:ss zzz")

$groups = @(
    [ordered]@{ code = "DS001"; order = 1; name = "공시정보"; file = "01-disclosure-information.md" },
    [ordered]@{ code = "DS002"; order = 2; name = "정기보고서 주요정보"; file = "02-periodic-report-key-information.md" },
    [ordered]@{ code = "DS003"; order = 3; name = "정기보고서 재무정보"; file = "03-periodic-report-financial-information.md" },
    [ordered]@{ code = "DS004"; order = 4; name = "지분공시 종합정보"; file = "04-ownership-disclosure.md" },
    [ordered]@{ code = "DS005"; order = 5; name = "주요사항보고서 주요정보"; file = "05-material-events.md" },
    [ordered]@{ code = "DS006"; order = 6; name = "증권신고서 주요정보"; file = "06-securities-registration.md" }
)

Add-Type -AssemblyName System.Net.Http
$httpClient = New-Object System.Net.Http.HttpClient
$httpClient.Timeout = [TimeSpan]::FromSeconds(30)
$httpClient.DefaultRequestHeaders.UserAgent.ParseAdd("HgFinance-OpenDART-DocsCollector/1.0")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-OpenDartHtml {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$MaxAttempts = 4
    )

    $url = if ($Path.StartsWith("http")) { $Path } else { "$baseUrl$Path" }
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $bytes = $httpClient.GetByteArrayAsync($url).GetAwaiter().GetResult()
            return [Text.Encoding]::UTF8.GetString($bytes)
        }
        catch {
            if ($attempt -eq $MaxAttempts) {
                throw "OpenDART 문서 호출 실패: $url ($($_.Exception.Message))"
            }
            Start-Sleep -Milliseconds (250 * [Math]::Pow(2, $attempt - 1))
        }
    }
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

function Get-TableRows {
    param(
        [Parameter(Mandatory = $true)][string]$Html,
        [Parameter(Mandatory = $true)][string]$Caption
    )

    $captionText = "<caption>$Caption</caption>"
    $captionIndex = $Html.IndexOf($captionText, [StringComparison]::OrdinalIgnoreCase)
    if ($captionIndex -lt 0) { return @() }

    $tableStart = $Html.LastIndexOf("<table", $captionIndex, [StringComparison]::OrdinalIgnoreCase)
    $tableEnd = $Html.IndexOf("</table>", $captionIndex, [StringComparison]::OrdinalIgnoreCase)
    if ($tableStart -lt 0 -or $tableEnd -lt 0) { return @() }

    $tableHtml = $Html.Substring($tableStart, ($tableEnd + 8) - $tableStart)
    $rows = @()
    foreach ($rowMatch in [regex]::Matches($tableHtml, "(?is)<tr[^>]*>(.*?)</tr>")) {
        $cells = @(
            [regex]::Matches($rowMatch.Groups[1].Value, "(?is)<t[dh][^>]*>(.*?)</t[dh]>") |
                ForEach-Object { ConvertTo-PlainText $_.Groups[1].Value }
        )
        if ($cells.Count -gt 0) { $rows += ,$cells }
    }
    return $rows
}

function Get-GroupApis {
    param(
        [Parameter(Mandatory = $true)][string]$Html,
        [Parameter(Mandatory = $true)][string]$GroupCode
    )

    $pattern = '(?is)<tr>\s*<td>(\d+)</td>\s*<td>(.*?)</td>\s*<td class="tl">(.*?)</td>\s*<td><a href="(/guide/detail\.do\?apiGrpCd=(DS\d+)&apiId=(\d+))"'
    $apis = @()
    foreach ($match in [regex]::Matches($Html, $pattern)) {
        if ($match.Groups[5].Value -ne $GroupCode) { continue }
        $apis += [pscustomobject]@{
            number = [int]$match.Groups[1].Value
            name = ConvertTo-PlainText $match.Groups[2].Value
            description = ConvertTo-PlainText $match.Groups[3].Value
            path = $match.Groups[4].Value
            id = $match.Groups[6].Value
        }
    }
    return @($apis | Sort-Object number)
}

function Add-MarkdownTable {
    param(
        [Parameter(Mandatory = $true)][System.Collections.ArrayList]$Lines,
        [Parameter(Mandatory = $true)][string[]]$Headers,
        [Parameter(Mandatory = $true)][object[]]$Rows
    )

    [void]$Lines.Add("| " + ($Headers -join " | ") + " |")
    [void]$Lines.Add("|" + (($Headers | ForEach-Object { "---" }) -join "|") + "|")
    foreach ($row in $Rows) {
        $cells = @($row | ForEach-Object { ConvertTo-MarkdownCell $_ })
        [void]$Lines.Add("| " + ($cells -join " | ") + " |")
    }
    [void]$Lines.Add("")
}

function Get-RequiredKeys {
    param([Parameter(Mandatory = $true)][object[]]$RequestRows)

    $keys = @()
    foreach ($row in @($RequestRows | Select-Object -Skip 1)) {
        if ($row.Count -ge 4 -and $row[3] -eq "Y") { $keys += $row[0] }
    }
    return $keys
}

function Add-ApiDetail {
    param(
        [Parameter(Mandatory = $true)][System.Collections.ArrayList]$Lines,
        [Parameter(Mandatory = $true)][object]$Api,
        [Parameter(Mandatory = $true)][string]$GroupCode
    )

    $detailUrl = "$baseUrl$($Api.path)"
    $detailHtml = Invoke-OpenDartHtml $Api.path
    $basicRows = @(Get-TableRows -Html $detailHtml -Caption "기본 정보")
    $requestRows = @(Get-TableRows -Html $detailHtml -Caption "요청 인자")
    $responseRows = @(Get-TableRows -Html $detailHtml -Caption "응답 결과")
    $detailTypeRows = @(Get-TableRows -Html $detailHtml -Caption "상세 유형")
    $anchor = "api-$($Api.id)"

    [void]$Lines.Add("<a id=""$anchor""></a>")
    [void]$Lines.Add("")
    [void]$Lines.Add("## $($Api.number). $($Api.name)")
    [void]$Lines.Add("")
    [void]$Lines.Add("- API ID: ``$($Api.id)``")
    [void]$Lines.Add("- 분류 코드: ``$GroupCode``")
    [void]$Lines.Add("- 기능: $(ConvertTo-MarkdownCell $Api.description)")
    [void]$Lines.Add("- 공식 상세: [OpenDART 원문]($detailUrl)")
    [void]$Lines.Add("")

    [void]$Lines.Add("### 기본 정보")
    [void]$Lines.Add("")
    if ($basicRows.Count -gt 1) {
        Add-MarkdownTable -Lines $Lines -Headers @("메서드", "요청 URL", "인코딩", "출력 형식") -Rows @($basicRows | Select-Object -Skip 1)
    }
    else {
        [void]$Lines.Add("공식 상세 페이지에서 기본 정보 표를 찾지 못했습니다.")
        [void]$Lines.Add("")
    }

    [void]$Lines.Add("### 요청 인자")
    [void]$Lines.Add("")
    if ($requestRows.Count -gt 1) {
        Add-MarkdownTable -Lines $Lines -Headers @("요청 키", "명칭", "타입", "필수", "값 설명") -Rows @($requestRows | Select-Object -Skip 1)
    }
    else {
        [void]$Lines.Add("공식 상세 페이지에서 요청 인자 표를 찾지 못했습니다.")
        [void]$Lines.Add("")
    }

    if ($detailTypeRows.Count -gt 1) {
        $normalizedDetailTypeRows = @()
        $currentDisclosureType = ""
        foreach ($row in @($detailTypeRows | Select-Object -Skip 1)) {
            if ($row.Count -eq 3) {
                $currentDisclosureType = $row[0]
                $normalizedDetailTypeRows += ,$row
            }
            elseif ($row.Count -eq 2) {
                $normalizedDetailTypeRows += ,@($currentDisclosureType, $row[0], $row[1])
            }
        }
        [void]$Lines.Add("### 공시 상세 유형")
        [void]$Lines.Add("")
        Add-MarkdownTable -Lines $Lines -Headers @("공시 유형", "상세 유형", "설명") -Rows $normalizedDetailTypeRows
    }

    [void]$Lines.Add("### 응답 필드")
    [void]$Lines.Add("")
    if ($responseRows.Count -gt 1) {
        Add-MarkdownTable -Lines $Lines -Headers @("응답 키", "명칭", "출력 설명") -Rows @($responseRows | Select-Object -Skip 1)
    }
    else {
        [void]$Lines.Add("공식 상세 페이지에서 응답 필드 표를 찾지 못했습니다.")
        [void]$Lines.Add("")
    }

    return [pscustomobject]@{
        requiredKeys = @(Get-RequiredKeys -RequestRows $requestRows)
        responseFieldCount = [Math]::Max(0, $responseRows.Count - 1)
        requestFieldCount = [Math]::Max(0, $requestRows.Count - 1)
    }
}

try {
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
    $groupResults = @()
    $totalApiCount = 0
    $totalRequestFieldCount = 0
    $totalResponseFieldCount = 0

    foreach ($group in $groups) {
        Write-Host ("[{0}/6] {1} 목록과 상세 수집" -f $group.order, $group.name)
        $mainPath = "/guide/main.do?apiGrpCd=$($group.code)"
        $mainHtml = Invoke-OpenDartHtml $mainPath
        $apis = @(Get-GroupApis -Html $mainHtml -GroupCode $group.code)
        $totalApiCount += $apis.Count

        $lines = New-Object System.Collections.ArrayList
        [void]$lines.Add("# OpenDART $($group.name) 전체 참조")
        [void]$lines.Add("")
        [void]$lines.Add("> [OpenDART 공식 개발가이드]($baseUrl$mainPath)를 $collectedAt 에 구조화한 개발용 참조입니다. 실제 연동 전 최신 계약과 예시는 공식 문서를 다시 확인합니다.")
        [void]$lines.Add("")
        [void]$lines.Add("## API 목록")
        [void]$lines.Add("")
        [void]$lines.Add("API $($apis.Count)개")
        [void]$lines.Add("")
        [void]$lines.Add("| 번호 | API | API ID | 기능 |")
        [void]$lines.Add("|---:|---|---|---|")
        foreach ($api in $apis) {
            $label = ConvertTo-LinkLabel $api.name
            [void]$lines.Add("| $($api.number) | [$label](#api-$($api.id)) | ``$($api.id)`` | $(ConvertTo-MarkdownCell $api.description) |")
        }
        [void]$lines.Add("")
        [void]$lines.Add("---")
        [void]$lines.Add("")

        foreach ($api in $apis) {
            Write-Host ("  - {0}/{1} {2}" -f $api.number, $apis.Count, $api.name)
            $detailResult = Add-ApiDetail -Lines $lines -Api $api -GroupCode $group.code
            $totalRequestFieldCount += $detailResult.requestFieldCount
            $totalResponseFieldCount += $detailResult.responseFieldCount
            [void]$lines.Add("---")
            [void]$lines.Add("")
        }

        Write-Utf8File -Path (Join-Path $OutputRoot $group.file) -Lines $lines.ToArray([string])
        $groupResults += [pscustomobject]@{
            code = $group.code
            name = $group.name
            file = $group.file
            apiCount = $apis.Count
        }
    }

    $index = New-Object System.Collections.ArrayList
    [void]$index.Add("# OpenDART Open API 전체 참조")
    [void]$index.Add("")
    [void]$index.Add("> [금융감독원 OpenDART 공식 개발가이드]($baseUrl/guide/main.do?apiGrpCd=DS001)를 $collectedAt 에 수집한 HgFinance 개발용 참조입니다. 최신 API 계약은 항상 공식 문서를 우선합니다.")
    [void]$index.Add("")
    [void]$index.Add("## 수집 결과")
    [void]$index.Add("")
    [void]$index.Add("| 항목 | 결과 |")
    [void]$index.Add("|---|---:|")
    [void]$index.Add("| 개발가이드 분류 | $($groups.Count)개 |")
    [void]$index.Add("| API | ${totalApiCount}개 |")
    [void]$index.Add("| 요청 인자 필드 | ${totalRequestFieldCount}개 |")
    [void]$index.Add("| 응답 필드 | ${totalResponseFieldCount}개 |")
    [void]$index.Add("")
    [void]$index.Add("## 전체 API 지도")
    [void]$index.Add("")
    [void]$index.Add("| 코드 | 분류 | API 수 | 상세 문서 |")
    [void]$index.Add("|---|---|---:|---|")
    foreach ($result in $groupResults) {
        [void]$index.Add("| ``$($result.code)`` | $($result.name) | $($result.apiCount) | [전체 요청·응답 계약]($($result.file)) |")
    }
    [void]$index.Add("")
    [void]$index.Add("## 공통 호출 계약")
    [void]$index.Add("")
    [void]$index.Add("- 기본 도메인: ``https://opendart.fss.or.kr``")
    [void]$index.Add("- 인증: 쿼리 파라미터 ``crtfc_key``에 발급받은 40자리 인증키를 전달한다.")
    [void]$index.Add("- 조회 API: 대부분 ``GET``이며 JSON과 XML을 제공한다.")
    [void]$index.Add("- 파일 API: 공시 원문, 고유번호, XBRL은 ZIP 바이너리 또는 XML 파일을 반환할 수 있다.")
    [void]$index.Add("- 회사 식별자: OpenDART ``corp_code``는 8자리이며 KRX ``stock_code``와 별도로 관리한다.")
    [void]$index.Add("- 정기보고서 코드: ``11011`` 사업보고서, ``11012`` 반기보고서, ``11013`` 1분기보고서, ``11014`` 3분기보고서다.")
    [void]$index.Add("- 공시 접수번호 ``rcept_no``는 공시 원문과 정정 이력을 연결하는 핵심 식별자다.")
    [void]$index.Add("")
    [void]$index.Add("## 메시지 코드")
    [void]$index.Add("")
    [void]$index.Add("| 코드 | 의미 | 처리 원칙 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| ``000`` | 정상 | 응답 저장 후 정규화한다. |")
    [void]$index.Add("| ``010`` | 등록되지 않은 키 | 비밀값 설정을 점검하고 재시도하지 않는다. |")
    [void]$index.Add("| ``011`` | 사용할 수 없는 키 | 키 상태를 확인하고 운영 알림을 발생시킨다. |")
    [void]$index.Add("| ``012`` | 접근할 수 없는 IP | 허용 IP와 배포 환경을 점검한다. |")
    [void]$index.Add("| ``013`` | 조회 데이터 없음 | 정상적인 빈 결과로 기록하되 요청 범위를 감사 가능하게 남긴다. |")
    [void]$index.Add("| ``014`` | 파일 없음 | 접수번호와 파일 생성 상태를 확인한다. |")
    [void]$index.Add("| ``020`` | 요청 제한 초과 | 지수 백오프하고 수집 일정을 늦춘다. |")
    [void]$index.Add("| ``021`` | 조회 회사 수 초과 | 회사를 최대 100개 이하로 분할한다. |")
    [void]$index.Add("| ``100`` | 필드 값 부적절 | 요청 검증 실패로 분류하고 자동 재시도하지 않는다. |")
    [void]$index.Add("| ``101`` | 부적절한 접근 | URL과 호출 방식을 점검한다. |")
    [void]$index.Add("| ``800`` | 시스템 점검 | 점검 종료 뒤 재시도한다. |")
    [void]$index.Add("| ``900`` | 정의되지 않은 오류 | 제한된 횟수만 재시도하고 장애 기록을 남긴다. |")
    [void]$index.Add("| ``901`` | 개인정보 보유기간 만료 키 | 계정과 키를 갱신하고 운영 알림을 발생시킨다. |")
    [void]$index.Add("")
    [void]$index.Add("## HgFinance 적용 원칙")
    [void]$index.Add("")
    [void]$index.Add("OpenDART는 실시간 가격 Feed가 아니라 기업 공시와 재무·지분·자본 이벤트를 제공하는 리서치 데이터 소스다. 각 에이전트가 OpenDART를 직접 반복 호출하지 않고 리서치본부 수집기가 한 번 수집해 전사 데이터 서비스로 배포한다.")
    [void]$index.Add("")
    [void]$index.Add('```text')
    [void]$index.Add("OpenDART API")
    [void]$index.Add("  -> Research Collector")
    [void]$index.Add("  -> Raw 원문/Object Storage")
    [void]$index.Add("  -> 검증·정규화·중복제거")
    [void]$index.Add("  -> Supabase research schema")
    [void]$index.Add("  -> Chunk/Embedding/pgvector")
    [void]$index.Add("  -> Research API와 Agentic RAG")
    [void]$index.Add('```')
    [void]$index.Add("")
    [void]$index.Add("### 수집 주기")
    [void]$index.Add("")
    [void]$index.Add("| 데이터 | 권장 시작 주기 | 수집 방식 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| 공시검색 | 장중 1~5분, 장외 10~30분 | 접수일과 최근 ``rcept_no`` 커서 기반 증분 수집 |")
    [void]$index.Add("| 고유번호 | 일 1회 | ZIP 전체 수신 후 변경된 회사만 upsert |")
    [void]$index.Add("| 기업개황 | 일 1회 또는 회사 변경 감지 시 | ``corp_code``별 캐시 갱신 |")
    [void]$index.Add("| 정기보고서·재무정보 | 신규 정기공시 감지 직후 | 보고연도와 보고서 코드 단위 수집 |")
    [void]$index.Add("| 지분·주요사항·증권신고서 | 관련 공시 감지 직후 | 이벤트 API 호출 후 원문과 함께 저장 |")
    [void]$index.Add("")
    [void]$index.Add("주기는 초기 운영값이다. OpenDART의 실제 제한과 수집 지연을 관측해 조정하며, 공식 문서의 일반적인 일일 요청 제한 수치를 보장값으로 하드코딩하지 않는다.")
    [void]$index.Add("")
    [void]$index.Add("### 저장 모델")
    [void]$index.Add("")
    [void]$index.Add("| 테이블 또는 저장소 | 역할 | 대표 키 |")
    [void]$index.Add("|---|---|---|")
    [void]$index.Add("| ``research.dart_corporations`` | 회사 식별자와 상장 종목 연결 | ``corp_code`` |")
    [void]$index.Add("| ``research.dart_filings`` | 공시 메타데이터와 정정 상태 | ``rcept_no`` |")
    [void]$index.Add("| ``research.dart_api_snapshots`` | API별 정규화 전후 응답 이력 | ``endpoint + request_hash + collected_at`` |")
    [void]$index.Add("| ``research.dart_financial_facts`` | 재무 계정과 기간별 값 | ``corp_code + bsns_year + reprt_code + fs_div + account_id`` |")
    [void]$index.Add("| ``research.dart_ownership_events`` | 임원·주요주주·대량보유 변화 | ``corp_code + rcept_no + event_key`` |")
    [void]$index.Add("| ``research.dart_material_events`` | 증자·합병·소송 등 자본 이벤트 | ``corp_code + rcept_no + event_type`` |")
    [void]$index.Add("| Object Storage | 공시 ZIP, XBRL, XML, 원본 JSON | ``source/opendart/date/rcept_no`` |")
    [void]$index.Add("| ``research.documents``와 pgvector | RAG용 청크와 임베딩 | ``document_id + chunk_index + embedding_model`` |")
    [void]$index.Add("")
    [void]$index.Add("OpenDART의 운영 원장은 Supabase PostgreSQL과 Object Storage에 둔다. TimescaleDB는 LS 가격·체결·호가와 파생 시계열을 위한 저장소이므로 OpenDART 원문 저장의 기본 위치로 사용하지 않는다.")
    [void]$index.Add("")
    [void]$index.Add("### 중복·정정·시점 관리")
    [void]$index.Add("")
    [void]$index.Add("1. 공시 목록은 ``rcept_no``로 멱등 upsert한다.")
    [void]$index.Add("2. API 응답은 요청 파라미터를 정렬한 ``request_hash``와 원문 ``content_hash``를 함께 저장한다.")
    [void]$index.Add("3. 정정공시는 이전 행을 덮어쓰지 않고 새 접수번호와 정정 관계를 보존한다.")
    [void]$index.Add("4. ``rcept_dt``와 ``collected_at``을 분리해 공시 시점과 시스템 관측 시점을 모두 남긴다.")
    [void]$index.Add("5. ``013`` 빈 결과도 수집 실행 기록에 남겨 누락과 정상 공백을 구분한다.")
    [void]$index.Add("6. 파싱 실패 시 원문은 보존하고 정규화 상태만 실패로 표시해 재처리한다.")
    [void]$index.Add("")
    [void]$index.Add("### Agentic RAG 계약")
    [void]$index.Add("")
    [void]$index.Add("- 청크 메타데이터에는 ``corp_code``, ``stock_code``, ``rcept_no``, 보고서 유형, 접수일, 정정 여부와 원문 위치를 넣는다.")
    [void]$index.Add("- 검색 결과는 원문 접수번호와 수집 시점을 인용해야 하며, 정정 전 문서는 기본 검색에서 제외한다.")
    [void]$index.Add("- 재무 숫자는 벡터 검색 결과만으로 계산하지 않고 정규화 테이블을 구조화 조회한다.")
    [void]$index.Add("- 리서치 Agent는 Research API를 통해 조회하고 API 키와 외부 호출 권한은 Collector에만 둔다.")
    [void]$index.Add("- 투자 판단에는 LS 시장 데이터의 기준 시각과 OpenDART 공시 관측 시각을 함께 기록한다.")
    [void]$index.Add("")
    [void]$index.Add("## 운영 체크리스트")
    [void]$index.Add("")
    [void]$index.Add("- ``OPENDART_API_KEY``는 Secret Manager 또는 배포 플랫폼의 Secret으로 주입하고 로그에 남기지 않는다.")
    [void]$index.Add("- 호출 타임아웃, 재시도 횟수, 지수 백오프와 일일 요청 예산을 설정한다.")
    [void]$index.Add("- 스케줄러 중복 실행을 막는 분산 Lock과 멱등 키를 적용한다.")
    [void]$index.Add("- 수집 지연, 오류 코드, 마지막 성공 커서, 파싱 실패율과 정정공시 처리 지연을 모니터링한다.")
    [void]$index.Add("- API 스키마 변경은 이 수집기를 다시 실행한 뒤 Git diff로 검토한다.")
    [void]$index.Add("")
    [void]$index.Add("## 재수집")
    [void]$index.Add("")
    [void]$index.Add("저장소 루트에서 다음 명령을 실행한다.")
    [void]$index.Add("")
    [void]$index.Add('```powershell')
    [void]$index.Add(".\scripts\collect_opendart_docs.ps1")
    [void]$index.Add('```')

    Write-Utf8File -Path (Join-Path $OutputRoot "README.md") -Lines $index.ToArray([string])
    Write-Host "완료: $totalApiCount APIs, $totalRequestFieldCount request fields, $totalResponseFieldCount response fields"
}
finally {
    if ($null -ne $httpClient) { $httpClient.Dispose() }
}

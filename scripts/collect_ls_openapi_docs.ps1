param(
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$workspaceRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $workspaceRoot "docs\06-integrations\ls-openapi"
}

$baseUrl = "https://openapi.ls-sec.co.kr"
$collectedAt = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTimeOffset]::UtcNow,
    "Korea Standard Time"
).ToString("yyyy-MM-dd HH:mm:ss zzz")

$groups = @(
    [ordered]@{ order = 1; name = "OAuth 인증"; folder = "01-oauth"; id = "ffd2def7-a118-40f7-a0ab-cd4c6a538a90" },
    [ordered]@{ order = 2; name = "업종"; folder = "02-industry"; id = "f82999f4-eb1a-4ead-a0b1-a4386e8721ab" },
    [ordered]@{ order = 3; name = "주식"; folder = "03-stock"; id = "73142d9f-1983-48d2-8543-89b75535d34c" },
    [ordered]@{ order = 4; name = "선물/옵션"; folder = "04-derivatives"; id = "2f1eea77-5606-4512-93c6-31b21d2ece90" },
    [ordered]@{ order = 5; name = "해외선물"; folder = "05-overseas-futures"; id = "c1ef0e8b-4666-4d8c-a77f-6ab488cfdb39" },
    [ordered]@{ order = 6; name = "해외주식"; folder = "06-overseas-stock"; id = "cdb7e1bc-f7c5-425c-8248-aa83dbb6919f" },
    [ordered]@{ order = 7; name = "기타"; folder = "07-misc"; id = "6ad419a5-f0ce-47c2-a52a-91685fa86a31" },
    [ordered]@{ order = 8; name = "실시간 시세 투자정보"; folder = "08-realtime-investment"; id = "cd909627-82e5-40c9-b313-1a8fd2d7b119" }
)

Add-Type -AssemblyName System.Net.Http
$httpClient = New-Object System.Net.Http.HttpClient
$httpClient.Timeout = [TimeSpan]::FromSeconds(30)
$httpClient.DefaultRequestHeaders.UserAgent.ParseAdd("PersonalHedgeFundAgent-DocsCollector/1.0")

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-LsJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$MaxAttempts = 4
    )

    $url = if ($Path.StartsWith("http")) { $Path } else { "$baseUrl$Path" }
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $bytes = $httpClient.GetByteArrayAsync($url).GetAwaiter().GetResult()
            $json = [Text.Encoding]::UTF8.GetString($bytes)
            return $json | ConvertFrom-Json
        }
        catch {
            if ($attempt -eq $MaxAttempts) {
                throw "LS Open API 문서 호출 실패: $url ($($_.Exception.Message))"
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
    $text = $text -replace "<[^>]+>", ""
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

function ConvertTo-FileNamePart {
    param([Parameter(Mandatory = $true)][string]$Value)

    $safe = $Value.ToLowerInvariant() -replace "[^a-z0-9-]", "-"
    $safe = $safe -replace "-{2,}", "-"
    $safe = $safe.Trim("-")
    if ([string]::IsNullOrWhiteSpace($safe)) { return "api" }
    return $safe
}

function ConvertTo-MarkdownLinkLabel {
    param([AllowNull()][object]$Value)

    $text = ConvertTo-MarkdownCell $Value
    return $text.Replace("[", "\[").Replace("]", "\]")
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

function Get-PropertyRows {
    param(
        [AllowNull()][object[]]$Properties,
        [Parameter(Mandatory = $true)][string]$BodyType,
        [Parameter(Mandatory = $true)][hashtable]$TypeMap
    )

    $rows = @()
    $selected = @($Properties | Where-Object { $_ -ne $null -and [string]$_.bodyType -eq $BodyType } | Sort-Object propertyOrder)
    foreach ($property in $selected) {
        $order = ConvertTo-PlainText $property.propertyOrder
        $rawField = [Net.WebUtility]::HtmlDecode([string]$property.propertyCd)
        $field = ($rawField -replace "^[\s\u00a0]*-?[\s\u00a0]*", "").Trim()
        $depth = if ([string]::IsNullOrWhiteSpace($order)) { 0 } else { [Math]::Max(0, ($order.Split(".").Count - 1)) }
        $typeCode = [string]$property.propertyType
        $typeName = if ($TypeMap.ContainsKey($typeCode)) { $TypeMap[$typeCode] } else { $typeCode }
        $required = if ([string]$property.requireYn -eq "Y") { "Y" } else { "N" }
        $rows += "| $(ConvertTo-MarkdownCell $order) | $depth | ``$(ConvertTo-MarkdownCell $field)`` | $(ConvertTo-MarkdownCell $property.propertyNm) | $(ConvertTo-MarkdownCell $typeName) | $(ConvertTo-MarkdownCell $property.propertyLength) | $required |"
    }
    return $rows
}

function Add-PropertySection {
    param(
        [Parameter(Mandatory = $true)][System.Collections.ArrayList]$Lines,
        [Parameter(Mandatory = $true)][string]$Title,
        [AllowNull()][object[]]$Properties,
        [Parameter(Mandatory = $true)][string]$BodyType,
        [Parameter(Mandatory = $true)][hashtable]$TypeMap
    )

    [void]$Lines.Add("### $Title")
    [void]$Lines.Add("")
    $rows = @(Get-PropertyRows -Properties $Properties -BodyType $BodyType -TypeMap $TypeMap)
    if ($rows.Count -eq 0) {
        [void]$Lines.Add("해당 필드가 없습니다.")
        [void]$Lines.Add("")
        return
    }
    [void]$Lines.Add("| 순서 | 깊이 | 필드 | 이름 | 형식 | 길이 | 필수 |")
    [void]$Lines.Add("|---:|---:|---|---|---|---:|:---:|")
    foreach ($row in $rows) { [void]$Lines.Add($row) }
    [void]$Lines.Add("")
}

try {
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

    $typeResponse = Invoke-LsJson "/api/codes/public/property_type"
    $typeMap = @{}
    foreach ($code in @($typeResponse.codes)) {
        $typeMap[[string]$code.key] = [string]$code.value
    }

    $manifestGroups = @()
    $totalApiCount = 0
    $totalTrCount = 0
    $totalPropertyCount = 0
    $propertyFailures = @()
    $documentationGaps = @()

    foreach ($group in $groups) {
        Write-Host ("[{0}/8] {1} API 목록 수집" -f $group.order, $group.name)
        $groupDirectory = Join-Path $OutputRoot $group.folder
        New-Item -ItemType Directory -Path $groupDirectory -Force | Out-Null

        $apis = @([object[]](Invoke-LsJson "/api/apis/public/api-list/$($group.id)") | Where-Object { $_ -ne $null })
        $groupApis = @()
        $groupTrCount = 0
        $apiIndex = 0

        foreach ($api in $apis) {
            $apiIndex++
            $totalApiCount++
            $apiId = [string]$api.id
            $apiDetailStatus = "공개 상세 제공"
            try {
                $apiDetail = Invoke-LsJson "/api/apis/public/$apiId"
            }
            catch {
                $apiDetail = $api
                $apiDetailStatus = "목록 메타데이터 대체"
                $documentationGaps += [ordered]@{
                    group = $group.name
                    api = $api.name
                    apiId = $apiId
                    reason = "API 상세 엔드포인트가 오류를 반환해 공개 목록 메타데이터를 사용함"
                }
            }

            $trs = @([object[]](Invoke-LsJson "/api/apis/guide/tr/$apiId") | Where-Object { $_ -ne $null })
            if ($trs.Count -eq 0 -and -not [string]::IsNullOrWhiteSpace([string]$api.extraParam)) {
                try {
                    $extraParam = ([string]$api.extraParam) | ConvertFrom-Json
                    if (-not [string]::IsNullOrWhiteSpace([string]$extraParam.tr_cd)) {
                        $requestLimit = ""
                        if (@($extraParam.ThroughputQuotaRule).Count -gt 0) {
                            $requestLimit = [string]@($extraParam.ThroughputQuotaRule)[0].requestLimit
                        }
                        $trs = @([pscustomobject]@{
                            id = "unavailable-$apiId"
                            trName = "$($extraParam.tr_cd) (공개 상세 미제공)"
                            trCode = [string]$extraParam.tr_cd
                            transactionPerSec = $requestLimit
                            reqExample = $null
                            resExample = $null
                            documentationUnavailable = $true
                        })
                        $documentationGaps += [ordered]@{
                            group = $group.name
                            api = $api.name
                            apiId = $apiId
                            trCode = [string]$extraParam.tr_cd
                            reason = "TR 코드는 공개 목록에 있으나 상세 필드 엔드포인트가 내용을 제공하지 않음"
                        }
                    }
                }
                catch {
                    $documentationGaps += [ordered]@{
                        group = $group.name
                        api = $api.name
                        apiId = $apiId
                        reason = "공개 extraParam을 해석하지 못함: $($_.Exception.Message)"
                    }
                }
            }
            $groupTrCount += $trs.Count
            $sourceUrl = "$baseUrl/apiservice?group_id=$($group.id)&api_id=$apiId"
            $apiFileName = "{0:D2}-{1}.md" -f $apiIndex, (ConvertTo-FileNamePart $apiId.Substring(0, 8))
            $apiFilePath = Join-Path $groupDirectory $apiFileName

            $lines = New-Object System.Collections.ArrayList
            [void]$lines.Add("# $(ConvertTo-PlainText $apiDetail.name)")
            [void]$lines.Add("")
            [void]$lines.Add("> LS증권 Open API 공개 문서를 $collectedAt 에 구조화한 개발용 참조입니다. 최신 계약과 공식 예제는 [원문 문서]($sourceUrl)를 기준으로 확인합니다.")
            [void]$lines.Add("")
            [void]$lines.Add("## API 기본 정보")
            [void]$lines.Add("")
            [void]$lines.Add("| 항목 | 값 |")
            [void]$lines.Add("|---|---|")
            [void]$lines.Add("| 대분류 | $(ConvertTo-MarkdownCell $group.name) |")
            [void]$lines.Add("| 프로토콜 | $(ConvertTo-MarkdownCell $apiDetail.protocolType) |")
            [void]$lines.Add("| HTTP 방식 | $(ConvertTo-MarkdownCell $apiDetail.httpMethod) |")
            [void]$lines.Add("| 운영 Domain | ``$(ConvertTo-MarkdownCell $apiDetail.domain)`` |")
            [void]$lines.Add("| 모의투자 Domain | ``$(ConvertTo-MarkdownCell $apiDetail.simulatedDomain)`` |")
            [void]$lines.Add("| 접속 경로 | ``$(ConvertTo-MarkdownCell $apiDetail.accessUrl)`` |")
            [void]$lines.Add("| Content-Type | ``$(ConvertTo-MarkdownCell $apiDetail.contentType)`` |")
            [void]$lines.Add("| API ID | ``$apiId`` |")
            [void]$lines.Add("| 문서 상세 상태 | $(ConvertTo-MarkdownCell $apiDetailStatus) |")
            [void]$lines.Add("| 포함 TR | $($trs.Count)개 |")
            [void]$lines.Add("")
            [void]$lines.Add("## TR 목록")
            [void]$lines.Add("")
            [void]$lines.Add("| 번호 | TR명 | TR 코드 | 초당 제한 | 요청 예제 | 응답 예제 |")
            [void]$lines.Add("|---:|---|---|---:|:---:|:---:|")

            $trDetails = @()
            $trIndex = 0
            foreach ($tr in $trs) {
                $trIndex++
                $totalTrCount++
                $trId = [string]$tr.id
                $properties = @()
                $documentationUnavailable = $tr.PSObject.Properties.Name -contains "documentationUnavailable" -and [bool]$tr.documentationUnavailable
                if (-not $documentationUnavailable) {
                    try {
                        $properties = @([object[]](Invoke-LsJson "/api/apis/guide/tr/property/$trId") | Where-Object { $_ -ne $null })
                    }
                    catch {
                        $propertyFailures += [ordered]@{ group = $group.name; api = $apiDetail.name; tr = $tr.trName; trId = $trId; error = $_.Exception.Message }
                    }
                }
                $totalPropertyCount += $properties.Count
                $hasRequestExample = if ([string]::IsNullOrWhiteSpace([string]$tr.reqExample)) { "없음" } else { "있음" }
                $hasResponseExample = if ([string]::IsNullOrWhiteSpace([string]$tr.resExample)) { "없음" } else { "있음" }
                [void]$lines.Add("| $trIndex | [$(ConvertTo-MarkdownLinkLabel $tr.trName)](#tr-$trIndex) | ``$(ConvertTo-MarkdownCell $tr.trCode)`` | $(ConvertTo-MarkdownCell $tr.transactionPerSec) | $hasRequestExample | $hasResponseExample |")
                $trDetails += [ordered]@{ index = $trIndex; value = $tr; properties = $properties; documentationUnavailable = $documentationUnavailable }
            }

            foreach ($trDetail in $trDetails) {
                $tr = $trDetail.value
                $properties = @($trDetail.properties)
                [void]$lines.Add("")
                [void]$lines.Add("---")
                [void]$lines.Add("")
                [void]$lines.Add(('<a id="tr-{0}"></a>' -f $trDetail.index))
                [void]$lines.Add("")
                [void]$lines.Add("## $($trDetail.index). $(ConvertTo-PlainText $tr.trName)")
                [void]$lines.Add("")
                [void]$lines.Add("- TR 코드: ``$(ConvertTo-MarkdownCell $tr.trCode)``")
                [void]$lines.Add("- TR ID: ``$([string]$tr.id)``")
                [void]$lines.Add("- 초당 호출 제한: ``$(ConvertTo-MarkdownCell $tr.transactionPerSec)``")
                [void]$lines.Add("- 공식 요청·응답 예제: [원문에서 확인]($sourceUrl)")
                [void]$lines.Add("- 필드 수: $($properties.Count)개")
                [void]$lines.Add("")

                if ([bool]$trDetail.documentationUnavailable) {
                    [void]$lines.Add("> LS 공개 목록에는 이 TR 코드와 호출 제한이 있지만 상세 요청·응답 필드 문서는 제공되지 않습니다.")
                    [void]$lines.Add("")
                }

                Add-PropertySection -Lines $lines -Title "요청 헤더" -Properties $properties -BodyType "req_h" -TypeMap $typeMap
                Add-PropertySection -Lines $lines -Title "요청 바디" -Properties $properties -BodyType "req_b" -TypeMap $typeMap
                Add-PropertySection -Lines $lines -Title "응답 헤더" -Properties $properties -BodyType "res_h" -TypeMap $typeMap
                Add-PropertySection -Lines $lines -Title "응답 바디" -Properties $properties -BodyType "res_b" -TypeMap $typeMap
            }

            Write-Utf8File -Path $apiFilePath -Lines @($lines)
            $groupApis += [ordered]@{
                id = $apiId
                name = [string]$apiDetail.name
                protocolType = [string]$apiDetail.protocolType
                httpMethod = [string]$apiDetail.httpMethod
                accessUrl = [string]$apiDetail.accessUrl
                file = "$($group.folder)/$apiFileName"
                sourceUrl = $sourceUrl
                trCount = $trs.Count
            }
            Write-Host ("  - {0}/{1} {2}: TR {3}개" -f $apiIndex, $apis.Count, $apiDetail.name, $trs.Count)
            Start-Sleep -Milliseconds 80
        }

        $manifestGroups += [ordered]@{
            id = $group.id
            name = $group.name
            folder = $group.folder
            apiCount = $groupApis.Count
            trCount = $groupTrCount
            apis = $groupApis
        }
    }

    $manifest = [ordered]@{
        source = "$baseUrl/apiservice"
        collectedAt = $collectedAt
        scope = "Public API metadata and complete request/response field schemas"
        counts = [ordered]@{
            groups = $manifestGroups.Count
            apis = $totalApiCount
            trs = $totalTrCount
            properties = $totalPropertyCount
            propertyFailures = $propertyFailures.Count
            documentationGaps = $documentationGaps.Count
        }
        propertyTypes = $typeMap
        groups = $manifestGroups
        failures = $propertyFailures
        documentationGaps = $documentationGaps
    }
    $manifestPath = Join-Path $OutputRoot "manifest.json"
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 20), $utf8NoBom)

    $indexLines = New-Object System.Collections.ArrayList
    [void]$indexLines.Add("# LS증권 Open API 전체 참조")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("> [LS증권 Open API 공식 문서]($baseUrl/apiservice)를 $collectedAt 에 수집한 개발용 인덱스입니다.")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("## 수집 결과")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("| 항목 | 결과 |")
    [void]$indexLines.Add("|---|---:|")
    [void]$indexLines.Add("| 대분류 | $($manifestGroups.Count)개 |")
    [void]$indexLines.Add("| API 묶음 | $totalApiCount 개 |")
    [void]$indexLines.Add("| TR | $totalTrCount 개 |")
    [void]$indexLines.Add("| 요청·응답 필드 | $totalPropertyCount 개 |")
    [void]$indexLines.Add("| 필드 조회 실패 | $($propertyFailures.Count)건 |")
    [void]$indexLines.Add("| 공식 상세 미제공 기록 | $($documentationGaps.Count)건 |")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("화면의 대분류 버튼, API 버튼, TR 상세 펼치기가 호출하는 공식 공개 API를 같은 순서로 순회했다. 각 API 문서에는 모든 TR과 요청 헤더, 요청 바디, 응답 헤더, 응답 바디 필드를 기록했다.")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("공식 사이트의 장문 설명과 대형 예제 Payload는 통째로 복제하지 않는다. 각 문서의 원문 링크에서 최신 설명과 예제를 확인하고, 이 저장소에서는 구현·검증에 필요한 전체 인터페이스 계약을 관리한다.")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("## 필드 형식 코드")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("| 코드 | 형식 |")
    [void]$indexLines.Add("|---|---|")
    foreach ($key in @($typeMap.Keys | Sort-Object)) {
        [void]$indexLines.Add("| ``$key`` | ``$($typeMap[$key])`` |")
    }
    [void]$indexLines.Add("")
    [void]$indexLines.Add("## 전체 API 목록")
    [void]$indexLines.Add("")
    foreach ($manifestGroup in $manifestGroups) {
        [void]$indexLines.Add("### $($manifestGroup.name)")
        [void]$indexLines.Add("")
        [void]$indexLines.Add("API $($manifestGroup.apiCount)개, TR $($manifestGroup.trCount)개")
        [void]$indexLines.Add("")
        [void]$indexLines.Add("| API | 프로토콜 | 방식 | 접속 경로 | TR |")
        [void]$indexLines.Add("|---|---|---|---|---:|")
        foreach ($manifestApi in $manifestGroup.apis) {
            [void]$indexLines.Add("| [$(ConvertTo-MarkdownLinkLabel $manifestApi.name)]($($manifestApi.file)) | $(ConvertTo-MarkdownCell $manifestApi.protocolType) | $(ConvertTo-MarkdownCell $manifestApi.httpMethod) | ``$(ConvertTo-MarkdownCell $manifestApi.accessUrl)`` | $($manifestApi.trCount) |")
        }
        [void]$indexLines.Add("")
    }
    [void]$indexLines.Add("## 재수집 방법")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("저장소 루트에서 다음 명령을 실행한다.")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("````powershell")
    [void]$indexLines.Add(".\scripts\collect_ls_openapi_docs.ps1")
    [void]$indexLines.Add("````")
    [void]$indexLines.Add("")
    [void]$indexLines.Add("수집 결과와 개수는 [manifest.json](manifest.json)에 기록된다. 재수집 전후의 API·TR·필드 개수 차이는 LS 문서 계약 변경으로 보고 검토한다.")

    Write-Utf8File -Path (Join-Path $OutputRoot "README.md") -Lines @($indexLines)

    if ($propertyFailures.Count -gt 0) {
        throw "수집은 완료됐지만 필드 조회 실패가 $($propertyFailures.Count)건 있습니다. manifest.json을 확인하세요."
    }

    Write-Host "완료: 대분류 $($manifestGroups.Count), API $totalApiCount, TR $totalTrCount, 필드 $totalPropertyCount"
}
finally {
    $httpClient.Dispose()
}

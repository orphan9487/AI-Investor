# D-Plan 撰寫指南

每個交易日提交一份 JSON 檔（D-Plan）。
D-Plan 就是你的 Agent 當天的完整決策過程：**看了什麼 → 讀到什麼 → 怎麼判斷 → 決定做什麼 → 下什麼單**。
完整範例見 `D-Plan_TEAM_042_2026-10-28.json`（正例）與 `D-Plan_TEAM_043_2026-10-28.json`（反例），
欄位格式以 `D-Plan.schema.json` 為準。

---

## 整體結構

```json
{
  "schema_version": "4.3",
  "doc_type": "D-Plan",
  "team_id": "你的隊伍代號",
  "trade_date": "2026-10-27",
  "sources":            [ ... ],   // ① 用了哪些資料
  "observations":       [ ... ],   // ② 從資料讀到哪些事實
  "market_view":        { ... },   // ③ 整體市場怎麼看
  "inferences":         [ ... ],   // ④ 個股推論
  "decisions":          [ ... ],   // ⑤ 要調整哪些持股
  "no_trade_decisions": [ ... ],   // ⑤ 不動的持股與理由
  "orders":             [ ... ],   // ⑥ 實際委託
  "agent_metadata":     { ... }    // ⑦ Agent 執行資訊
}
```

寫作原則只有一條：**下層必須引用上層**。每筆委託對應一筆決策、每筆決策引用推論、
每筆推論引用事實、每筆事實引用資料來源，不能有斷環。

時間格式一律 `2026-10-27T07:52:36+08:00`（台北時區）。不要加規格以外的欄位。

**檔名規約**：`D-Plan_<team_id>_<trade_date>.json`，例如 `D-Plan_TEAM_042_2026-10-28.json`。
檔名中的隊伍代號與日期必須和 JSON 內的 `team_id`／`trade_date` 一致。
字元只用 `A-Z a-z 0-9 _ -`；檔名不帶時間戳。同日重繳使用同一檔名。

### ID 命名規則

| 欄位 | 格式 | 範例 | 說明 |
|---|---|---|---|
| `source_id` | `S` + 流水號 | `S1`…`S999` | 資料來源 |
| `obs_id` | `O` + 流水號 | `O1`…`O999` | 事實 |
| `inf_id` | `I` + 流水號 | `I1`…`I999` | 推論 |
| `decision_id` | `D` + 流水號 | `D1`…`D999` | 決策 |
| `ticker` | 4 位數字 | `2330` | 股票代號 |
| `team_id` | 主辦方配發 | `TEAM_042` | 照抄，勿自行改動 |

流水號規則：大寫前綴＋數字（1–3 位），**從 1 開始、連續遞增、不跳號、不加前導零**
（寫 `S1` 不是 `S01`）；同一層內不得重複。每天的 D-Plan 獨立編號，隔日從 1 重新起算。
引用欄位（`source_ref`、`premise_refs`、`basis_refs`、`inference_refs`、`reason_refs`、
`decision_ref`）填的 id 必須真實存在於對應層，打錯一個字就會造成引用斷裂。

---

## ⓪ 賽前準備 — ETF 公開說明書

向主辦方繳交官方格式的「ETF 公開說明書」每隊一份：ETF 名稱、投資主題、投資理念。

提醒：**之後每天的 D-Plan 都要與這份文件的宣告保持關聯**。

## ① sources — 用了哪些資料

每筆資料來源一個項目。

```json
{
  "source_id": "S1",                          // 自編流水號 S1, S2, ...
  "authority": "twse",                        // 來源類型，見下表
  "url": "https://openapi.twse.com.tw/...",
  "content_as_of": "2026-10-26T18:30:00+08:00"  // 這份內容「描述的時點」（非抓取時間）
}
```

| authority 填什麼 | 適用 |
|---|---|
| `twse` | 台灣證券交易所（**上市**個股價量、三大法人、加權指數） |
| `tpex` | 證券櫃檯買賣中心（**上櫃**個股價量、櫃買指數） |
| `taifex` | 期交所（台指期夜盤、選擇權） |
| `mops` | 公開資訊觀測站（財報、月營收、重大訊息） |
| `fininst` | 投信、券商、銀行等**金融機構**發布的研究報告、基金/ETF 持股資料 |
| `media` | 新聞媒體的**報導內容**（鉅亨網新聞、經濟日報等；建議加 `archive_url`） |
| `vendor` | 行情/數據**服務商**的 API 或數據（CMoney、富果 Fugle、Yahoo Finance、美股/ADR/匯率行情） |
| `other` | 不屬於以上類別的來源（部落格、公開資料集等）；建議附 `archive_url` |

`twse` 和 `tpex` 是兩個不同機構、兩套代碼，上市股的價量填 `twse`、上櫃股填 `tpex`，
不要混用。

**數位資訊源怎麼分類？看「這筆內容是什麼」，不是看網站名字。** 同一個平台常常跨類：
鉅亨網的**新聞報導**填 `media`，它轉載的行情表屬 `vendor`；CMoney 的**選股數據/API** 填
`vendor`，它的專欄文章屬 `media`；富果（Fugle）的**行情 API** 填 `vendor`；投信官網的
**基金月報、研究報告**填 `fininst`。拿不準時：報導、觀點 → `media`；數字、行情、資料表 →
`vendor`；出自金融機構名義的研究產出 → `fininst`。

新聞類來源（media/vendor）**強烈建議**抓取當下順手送 web.archive.org 或 archive.today
存檔，並把存檔連結填進 `archive_url`，原始網頁可能事後變動，存檔是新聞內容的唯一佐證。
另建議自行留存各來源的原始內容備查。

## ② observations — 讀到哪些事實

把來源中用得到的**事實**逐條列出：一句話描述＋關鍵數字。只寫事實，不寫判斷
（「SOX 跌 1.8%」可以；「所以半導體會弱」屬於下一層）。

```json
{
  "obs_id": "O2",
  "source_ref": ["S2"],                        // 出自哪筆來源
  "statement": "SOX -1.8%、NDX -0.9%；半導體權值普遍收黑",
  "values": { "SOX_chg_pct": -1.8, "NDX_chg_pct": -0.9 }   // 只放數字
}
```

## ③ market_view — 整體市場怎麼看（每天一份）

先於個股判斷，交代今天的大方向：依據哪些市場級事實、研判走向、決定姿態，
以及這個姿態**落實到部位上的意思**。

```json
{
  "basis_refs": ["O1", "O2", "O3"],            // 市場級事實（大盤、美股、夜盤…）
  "logic": "美股半導體回檔但降息預期升溫，判定中性震盪…",
  "regime": "neutral",                         // risk_on / neutral / risk_off
  "stance": "neutral",                         // aggressive / neutral / defensive
  "posture": {
    "net_exposure_intent": "hold",             // increase=淨買超 / hold=大致持平 / reduce=淨賣超
    "target_cash_pct_range": [0.03, 0.10]      // 今天現金比想落在的區間
  },
  "counter_evidence": "SOX 若連跌兩日將轉為 risk_off"   // 反方訊號；沒有就填 null
}
```

**regime — 市場走向研判**

| 代碼 | 意義 |
|---|---|
| `risk_on` | 風險偏好：訊號偏多，市場處於進攻環境 |
| `neutral` | 中性：訊號混雜或無方向，震盪整理 |
| `risk_off` | 風險趨避：訊號偏空，市場處於防禦環境 |

**stance — 本日操作姿態**

| 代碼 | 意義 |
|---|---|
| `aggressive` | 積極：提高曝險、追逐機會 |
| `neutral` | 中性：維持既有配置，僅做必要調整 |
| `defensive` | 保守：降風險、偏防禦部位 |

`regime` 是「你怎麼看市場」、`stance` 是「你打算怎麼做」，通常同向，但可以不同
（例如判定 risk_off 但持倉已經很低，stance 填 neutral 即可），不要求硬性對應。

**posture.net_exposure_intent — 淨曝險意圖**

| 代碼 | 意義 | 委託必須滿足 |
|---|---|---|
| `increase` | 提高股票曝險 | 買進金額 > 賣出金額（淨買超） |
| `hold` | 大致持平（換股不改總曝險） | \|買−賣\| ≤ 2% 淨值 |
| `reduce` | 降低股票曝險 | 賣出金額 > 買進金額（淨賣超） |

注意：`posture` 是承諾不是敘述，委託加總（以前一日交易所收盤價估算）要與宣告一致：
宣告 `increase` 委託就要淨買超、`reduce` 要淨賣超、`hold` 則淨買賣金額差在 2% 淨值以內；
以同一價格估算的委託後現金比，也要落在你宣告的區間內。
這裡用的是前日收盤價的「預估值」（實際成交是當日均價，撰寫時還不存在）——
實際成交後現金比會有小幅偏移，所以宣告區間和下單請對 0% 和 25% 的上下限留點緩衝。

## ④ inferences — 個股推論

從事實推到個股結論。每筆要引用事實（`premise_refs`），`logic` 把推導過程寫清楚，
並與你們公開說明書宣告的主題與因子保持一致。

```json
{
  "inf_id": "I1",
  "premise_refs": ["O2", "O3", "O4"],
  "logic": "SOX -1.8% 且 TSM ADR -1.5%，夜盤同向下跌，符合本 ETF 隔夜風險控管邏輯…",
  "counter_evidence": "台積電基本面展望未變，故僅減碼不出清"   // 沒有就填 null
}
```

## ⑤ decisions / no_trade_decisions — 決定做什麼

**每一檔持股今天都要有交代**：要調整的寫進 `decisions`，不動的寫進 `no_trade_decisions`。
昨日每一檔持股必須出現在兩者之一，同一檔不能兩邊都寫、也不能重複；
今天新買進的寫在 `decisions`（action=BUY）。

```json
// 調整
{
  "decision_id": "D1",
  "ticker": "2330",
  "action": "TRIM",                  // BUY 新建 / ADD 加碼 / TRIM 減碼 / SELL_ALL 出清
  "target_weight": 0.23,             // 目標權重（2330 上限 25%，其他個股 10%）
  "inference_refs": ["I1"],          // 依據哪些推論，不能空
  "funding_for": ["D2"]              // 選填：這筆賣出是為了支應哪筆買進（資金調度宣告）
}
// 不動
{ "ticker": "2317", "reason_refs": ["I3"] }
```

**action — 決策動作**

| 代碼 | 意義 |
|---|---|
| `BUY` | 新建倉（原持股為 0） |
| `ADD` | 加碼既有持股 |
| `TRIM` | 減碼既有持股（仍保留部位） |
| `SELL_ALL` | 全數出清 |

記得競賽限制：持股 20–30 檔、只能買 150 檔名單內的股票、現金要保持在 0–25% 之間。

### 資金調度型賣出怎麼寫

賣股有兩種理由：**看壞它**（訊號驅動），或**為了買別檔而騰錢**（資金調度），兩種都必須有
推論，但寫法不同。資金調度型請在決策加上 `funding_for` 指向被支應的買進決策，推論則說明
「為何選這檔讓位」的選擇邏輯（持股中動能最弱、偏離目標最小、賣它對組合影響最小），
且 L1 要有支撐選擇的事實（例如「2412 近 20 日相對大盤 -3.2%，為持股中最弱」，來源引交易所價量）。
兩種動機可以並存（既有風險訊號、又順勢騰錢），照實寫即可。

`funding_for` 是宣告不是裝飾，要滿足兩件事：①指向的必須是**同一天的 BUY/ADD 決策**；
②**缺口必須真的存在**，把你標記的調度賣出拿掉重算，現金比會跌破你在 market_view
宣告的下限。沒有資金缺口就不要標記；確為調度的賣出，照實標記。

## ⑥ orders — 實際委託

由 decisions 機械換算出的下單清單，**必須用官方演算法算，不能手調**：

```
目標股數 = floor( target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000 ) × 1000
委託股數 = 目標股數 − 目前持有股數     （正=BUY，負=SELL）
```
**文件繳交**頁面可以下載資料包取得前日淨值與持股，價格用交易所公布的前日收盤價。

```json
{ "ticker": "2330", "side": "SELL", "shares": 2000, "decision_ref": "D1" }
```

股數必須是 1000 的倍數（整股）。只能現股買賣，不能放空、當沖。

> ⚠️ **賣出股數不得超過你實際持有的股數**。
>
> 委託未必全部成交，**持股以系統的結算結果為準**。
>
> 每天繳交前，先到參賽者後台的**文件繳交**頁面下載資料包。以資料包內的系統結算結果為基準決定可以賣出多少張股票，避免超賣。

**side — 委託方向**：`BUY` 買進 / `SELL` 賣出。由官方演算法從 decisions 推導而來，
不用自己選；注意它跟 ⑤ 的 `action: "BUY"` 是不同欄位，action 的 BUY 是「新建倉」
這個決策動作，side 的 BUY 是委託方向。

## ⑦ agent_metadata — Agent 執行資訊

```json
{
  "model_provider": "anthropic",
  "model_version": "claude-opus-4-7",
  "run_started_at":  "2026-10-27T05:02:10+08:00",
  "run_completed_at": "2026-10-27T07:52:36+08:00",  // 須早於（≤）server 收件時間
  "code_version": "git:a1b2c3d",               // 當日程式碼版本識別
  "input_tokens": 48200,                        // 選填：本次執行的用量紀錄
  "output_tokens": 10900,                       // 選填
  "tool_use_count": 14                          // 選填
}
```

**model_provider 可填值**：

`anthropic` / `openai` / `google` / `xai` / `deepseek` / `alibaba` /
`meta` / `mistral` / `cohere` / `amazon` / `microsoft` / `nvidia` / `taide` / `other`

自架開放權重模型請填**釋出權重的組織**（Llama → `meta`、Qwen → `alibaba`、
TAIDE → `taide`）。清單未涵蓋者填 `other`，並在 `model_version` 寫明是哪一個。

**這一節是自報的執行紀錄**，用於趨勢與統計分析。`code_version` 用於與日後繳交的
程式碼對帳，請確保它能對應到當天實際執行的程式版本。自報內容須誠實：
`run_completed_at` 不得晚於實際提交時間。

**code_version**：git commit（`git:a1b2c3d`）、版本號（`v1.4.2`）、或當天程式碼
打包 zip 的 sha256 前 12 碼都可以，重點是晉級後資格審查時，你們要能拿出「這個識別對應
的那一版程式碼」。不用 git 的話，建議每天把程式碼打包留存並記下雜湊，格式不拘，
但限英數與` _ . : / -`，長度 1–64。
注意：**影響決策行為的參數和提示詞模板都算程式碼的一部分**，改參數就是改版，要換新的版本識別。

---

## 提交前自我檢查

1. 引用鏈完整：每筆 order→decision→inference→observation→source 都對得上，id 沒有打錯。
2. 委託是用官方公式從 decisions 算出來的，股數為千股倍數。
3. 委託加總與 `market_view.posture` 相符（淨買賣方向、現金區間）。
4. 成交後持股 20–30 檔、個股權重不超限、現金不為負且低於 25%。
5. 主題一致性：委託後的主題產業占比沒有明顯偏低，決策要對得回公開說明書的宣告。
6. 資金調度賣出有標記 `funding_for`，且拿掉調度賣出後現金真的會跌破宣告下限（缺口存在）。
7. 覆蓋完整：昨日每一檔持股都出現在 `decisions` 或 `no_trade_decisions`（擇一，不重複）。
8. 檔名符合 `D-Plan_<team_id>_<trade_date>.json`，且與 JSON 內容一致。

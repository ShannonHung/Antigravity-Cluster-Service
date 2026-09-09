# Drain 參數測試文件

`POST /api/v1/clusters/{cluster}/nodes/{node}/drain` 四個參數的完整測試說明:
每個參數各自守住什麼、開與不開分別會發生什麼、以及在真實 k3s 叢集上的實測結果。

- 單元測試:`tests/unit/test_node_service.py`(mock `CoreV1Api`,不需叢集)
- e2e 測試:`tests/e2e/test_drain_e2e.py`(需要真的叢集)
- 情境資源:`tests/e2e/manifests/drain-scenarios.yaml`

---

## 設計前提

這一版 drain 有兩個和舊版不同的行為,測試都圍繞著它們:

**1. 擋在前面,不是擋在中間。** `force` 和 `delete_emptydir_data` 是「解除保護」的旗標。
只要有任何一個 pod 沒被許可移除,整個 drain 在**驅逐第一個 pod 之前**就回 `400 DRAIN_BLOCKED`,
並且一次列出**所有**違規的 pod 和**所有**需要補上的旗標。

理由:drain 的目的是清空節點,清一半沒有意義 — 已經被殺掉的 pod 救不回來,而節點仍然不是空的。
一次講完所有問題,呼叫者才不用「補一個旗標、再撞一次牆」來回好幾趟。

**2. 等不完不是錯誤。** 舊版等待 pod 終止超過 25 秒就丟 `504 DRAIN_TIMEOUT`。
現在回 `200`,附上 `still_terminating`(還沒走的 pod)、`node_emptied`(節點是否已清空)。

理由:沒有任何東西 timeout — 所有驅逐指令都被接受了。一個 `terminationGracePeriodSeconds: 120`
的 pod 正在**按照它的設定**優雅關閉,這是正常的 Kubernetes 行為,不是故障。

---

## 四個參數各自守住什麼

| 參數 | 守住的 pod 種類 | 不開會怎樣 | 開了會怎樣 |
|---|---|---|---|
| `force` | 沒有 ownerReferences 的裸 pod | `400`,`required_options: {force: true}` | 裸 pod 被驅逐 |
| `delete_emptydir_data` | 掛載 emptyDir volume 的 pod | `400`,`required_options: {delete_emptydir_data: true}` | emptyDir pod 被驅逐 |
| `disable_eviction` | (不守任何 pod) | 走 Eviction API,被 PDB 擋住時回 `409` | 改用 raw DELETE,直接繞過 PDB |
| `grace_period_seconds` | (不守任何 pod) | 用 pod 自己的 `terminationGracePeriodSeconds` | `0` = 立刻刪除,回應 `forced_deletion: true` |

**關鍵區別**:前兩個決定 pod **可不可以**被移除(影響 blocking),後兩個決定 pod **怎麼**離開
(不影響 blocking)。所以 `disable_eviction=true` 不會讓一個裸 pod 變成可驅逐 —
它仍然需要 `force`。

### 永遠被跳過的三種 pod(沒有任何旗標能碰)

- **DaemonSet pod** — 驅逐了 controller 也會馬上放回來,沒有意義
- **Mirror / static pod** — 由 kubelet 從節點上的檔案管理,API server 刪不掉
- **已完成的 pod**(`Succeeded` / `Failed`)— 已經沒有在跑,沒有東西要搬

這三種的判斷**先於**保護檢查,所以一個「用 emptyDir 的 DaemonSet pod」不會要求
`delete_emptydir_data`,一個「已完成的裸 pod」也不會要求 `force`。

---

## 測試情境

`tests/e2e/manifests/drain-scenarios.yaml` 在 `test-drain` namespace 建立八個情境,
全部用 `nodeName` 釘在同一個節點上:

| 資源 | 種類 | 用來測 |
|---|---|---|
| `clean-web` | Deployment | 對照組:不需要任何旗標就能驅逐 |
| `bare-pod` | 裸 Pod | `force` |
| `emptydir-cache` | Deployment + emptyDir | `delete_emptydir_data` |
| `bare-emptydir` | 裸 Pod + emptyDir | 同時違反兩條規則 → 一次回報兩個旗標 |
| `pdb-guarded` | Deployment + PDB(`minAvailable: 1`,1 replica)| `disable_eviction` |
| `slow-terminator` | Deployment,`trap '' TERM`,grace 120s | `grace_period_seconds` / `still_terminating` |
| `drain-daemon` | DaemonSet | 永遠跳過 |
| `completed-job` | 裸 Pod,跑完就結束 | 永遠跳過(且證明「完成」的判斷早於「裸 pod」)|

`pdb-guarded` 的 PDB 設 `minAvailable` 等於 replica 數,代表 Eviction API **永遠**不會
允許自願性中斷 — 它會一直回 429。這是只有真叢集能驗證的假設。

---

## 怎麼跑

```bash
# 單元測試(不需叢集,CI 跑這個)
make test

# e2e 測試(需要叢集)
make test-e2e
```

e2e 測試預設打 `k3d-mycluster` 叢集的 `k3d-mycluster-agent-1` 節點,可用環境變數覆寫:

```bash
E2E_CLUSTER=my-cluster E2E_DRAIN_NODE=my-node make test-e2e
```

**注意**:e2e 測試會真的 cordon 並清空目標節點。目標節點上不應該有任何無法被重新排程的
東西(hostPath 資料、local-path PVC)。測試結束時 fixture 一定會 uncordon 並刪掉
`test-drain` namespace,即使測試失敗也一樣。

叢集連不上時整個 suite 會 skip(而不是噴一堆連線錯誤),所以在沒開叢集的機器上跑
`make test-e2e` 會得到一行清楚的 skip 訊息。

`e2e` marker 已經註冊在 `pyproject.toml`,`make test` 用 `-m 'not e2e'` 排除它們,
CI 不會因為沒有叢集而變紅。

---

## 實測結果

### 執行環境

| 項目 | 值 |
|---|---|
| 叢集 | k3d `mycluster` (k3s v1.33.6+k3s1) |
| 節點 | server-0 (control-plane) + agent-0 + agent-1 |
| 被 drain 的節點 | `k3d-mycluster-agent-1` |
| 執行日期 | 2026-09-10 |

### 單元測試

```
$ make test
218 passed, 10 deselected in 13.09s
```

`test_node_service.py` 從 54 個測試增加到 96 個,新增的主要是四個參數的完整組合矩陣
(`test_drain_option_matrix`,2×2×2×3 = 24 組)。`10 deselected` 是被 `-m 'not e2e'`
排除的 e2e 測試 — CI 不會因為沒有叢集而失敗。

### e2e 測試(真實叢集)

```
$ make test-e2e
tests/e2e/test_drain_e2e.py::test_plain_drain_is_blocked_by_both_guards PASSED           [ 10%]
tests/e2e/test_drain_e2e.py::test_blocked_drain_destroys_nothing PASSED                  [ 20%]
tests/e2e/test_drain_e2e.py::test_blocked_drain_still_cordons_the_node PASSED            [ 30%]
tests/e2e/test_drain_e2e.py::test_force_alone_still_blocked_by_emptydir PASSED           [ 40%]
tests/e2e/test_drain_e2e.py::test_emptydir_alone_still_blocked_by_unmanaged PASSED       [ 50%]
tests/e2e/test_drain_e2e.py::test_pdb_blocks_eviction_without_disable_eviction PASSED    [ 60%]
tests/e2e/test_drain_e2e.py::test_disable_eviction_bypasses_pdb PASSED                   [ 70%]
tests/e2e/test_drain_e2e.py::test_sigterm_ignoring_pod_is_reported_still_terminating PASSED [ 80%]
tests/e2e/test_drain_e2e.py::test_grace_zero_kills_sigterm_ignoring_pod_immediately PASSED  [ 90%]
tests/e2e/test_drain_e2e.py::test_daemonset_and_completed_pods_survive_maximum_force PASSED [100%]

======================== 10 passed in 154.94s (0:02:34) ========================
```

測試結束後叢集狀態已還原:`k3d-mycluster-agent-1` 回到 `Ready` 且未 cordon,
`test-drain` namespace 已刪除。

### 逐項驗證結果

| 驗證項目 | 結果 | 在真實叢集上證明了什麼 |
|---|---|---|
| 不帶旗標 → `400` | ✅ | `required_options` 同時列出 `force` 和 `delete_emptydir_data`,`bare-emptydir` 的 `reasons` 是 `["unmanaged", "emptydir"]` — 一次講完,不用來回試 |
| 被擋時什麼都沒刪 | ✅ | drain 前後 namespace 內的 pod 集合完全相同 |
| 被擋時仍然 cordon | ✅ | `node.spec.unschedulable == True` |
| 只開 `force` | ✅ | 仍被擋,但 `required_options` 只剩 `delete_emptydir_data` |
| 只開 `delete_emptydir_data` | ✅ | 仍被擋,`required_options` 只剩 `force` |
| PDB 擋住 eviction | ✅ | **Eviction API 真的回 429**,被轉成 `409` 並在訊息中指名 `pdb-guarded` 和 `disable_eviction` |
| `disable_eviction` 繞過 PDB | ✅ | raw DELETE 成功,`pdb-guarded` 出現在 `drained_pods` |
| SIGTERM 被忽略的 pod | ✅ | `node_emptied: false`,`slow-terminator` 出現在 `still_terminating`,**回 200 不是 504** |
| `grace_period_seconds=0` | ✅ | 同一個 pod 立刻消失,`still_terminating` 為空,`forced_deletion: true` |
| 全部旗標全開 | ✅ | DaemonSet pod 和 `completed-job` **依然存活** — 沒有任何旗標能碰它們 |

### 過程中發現並修掉的問題

**1. `force` 和 `delete_emptydir_data` 是死參數。** 這是這次工作的起點:兩個參數在
`DrainOptions` 有定義、在 timeout 的建議訊息裡被推薦,但 `drain()` 從來沒有讀過它們。
實際行為等於永遠 `--force --delete-emptydir-data` — 呼叫者以為有保護,其實沒有。

**2. `NodeMetadataData` 不存在。** `app/api/v1/nodes.py` 的 `patch_node_labels` 和
`patch_node_annotations` 回傳型別註解寫的是 `ApiResponse[NodeMetadataData]`,但這個名字
在 `kubernetes_models.py` 裡沒有定義也沒有 import。因為 `from __future__ import annotations`
讓註解變成字串,所以 import 時不會爆,但 FastAPI 解析回傳型別時會失敗。順手改成正確的
`NodeLabelsData` / `NodeAnnotationsData`。

**3. (測試自身的 bug)namespace 刪除競態。** 第一版 fixture 用 `--wait=false` 刪 namespace,
下一個測試馬上又用 `--wait=true` 刪一次。但 `slow-terminator` 會 trap SIGTERM 並撐滿 120 秒
grace period,namespace 因此長時間卡在 `Terminating`。往 `Terminating` 的 namespace 做
`kubectl apply` **會回報成功**,但資源隨即被回收 — 於是下一個測試永遠等不到它要的 pod。
修法是 teardown 時用 `--grace-period=0 --force` 強制刪除 pod,並且真的等到 namespace 消失
為止。這也剛好示範了為什麼 `grace_period_seconds=0` 這個參數存在。

### 已知限制

- **Mirror / static pod 沒有 e2e 覆蓋。** 在 k3d 上要建立 static pod 必須把 manifest 塞進節點
  容器的 `/var/lib/rancher/k3s/agent/pod-manifests/`,成本遠高於價值。這條路徑由單元測試
  (`test_drain_mirror_pod_with_emptydir_is_skipped_not_blocked`)覆蓋。
- **e2e 測試是循序的,約需 2.5 分鐘。** 每個測試都要重建情境並等待 pod 就緒,其中兩個還要
  等滿 25 秒的 wait budget。不適合放進每次 commit 的 pre-commit hook。
- **`still_terminating` 的內容取決於 wait budget。** `DRAIN_DEFAULT_TIMEOUT_SECONDS` 若被
  調到大於 120 秒,`test_sigterm_ignoring_pod_is_reported_still_terminating` 就會失敗 —
  因為 pod 會在 budget 內自然死掉。這是測試對設定的合理依賴,不是脆弱性。


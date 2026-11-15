<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 参与开发

[主 README](README.md) · [文档索引](docs/README.md) · [架构](docs/architecture.md)

## 环境

```bash
python3 --version    # 需要 ≥ 3.11
node --version       # 需要 ≥ 22
cd tools && npm install && cd ..
cp config.example.toml config.toml
./run.sh deps check  # 六项全 OK
```

零第三方 Python 依赖，不要为了图方便引入 `requests` / `pydantic` 之类 —— 只用标准库是
本项目的硬约束（独立服务要能在裸 macOS 上跑起来）。

## 测试

```bash
python3 -m unittest discover -s tests -q
```

- 单测**必须全程 mock 掉 CDP**，不许依赖本机客户端状态、不许真的点按钮。
- 新增判据就要新增用例：签到翻日、补签三态、缺口交叉校验、版本红线、端口归属都有对应用例，
  照着写比读文档快。
- 提交前至少跑一遍；改到真机行为时再补一次 `./run.sh inspect` / `once`，并在 PR 里贴输出。

## 代码约定

| 约定 | 说明 |
|---|---|
| 端口只有一个注入点 | `cli.inject_port()`，不要在别处写 9222 或直接读环境变量 |
| 版本红线只有一处 | `compat.py`，不要在业务代码里散落 `5.11` |
| 状态只认结构 | 用 class / `disabled` / 倒计时文本判定，不要按界面文案猜 |
| 时间统一北京时间 | 走 `timeutil`，naive 时间按北京时间解释 |
| 领取必须幂等 | 经 `state.claim_once`，重复运行不能重复领 |
| 失败不换通道 | CDP 不可达就报错并给修复命令，不静默降级 |

## 提交

- 一个 commit 一件事，message 用 `feat(scope):` / `fix(scope):` / `docs:` / `test:` 前缀。
- 改到行为（判据、顺序、退避、退出码）时同步更新 `docs/changelog.md` 与相关文档。
- 新增翻译文件沿用 `README_<小写语言码>.md`（如 `README_ja.md`），需要地区才写
  `README_pt-BR.md`；**不要混用分隔符**（别再加 `README.en.md` / `README-ja.md`），依据见
  [docs/README.md](docs/README.md#文档语言与命名约定)。
- 不提交 `config.toml`、`progress.txt`、`*.sqlite3`、`*.png`、`tools/node_modules/`、`.tmp/`。

## 依赖许可（硬规则）

本项目采用 MIT，依赖树也必须是**完全开源、可商用**，不能出现「源可得但非开源」的许可。

- 允许：OSI 认证的宽松许可 —— `MIT` / `Apache-2.0` / `BSD-2-Clause` / `BSD-3-Clause` / `ISC`。
- **禁止入依赖树**：BUSL、FSL、PolyForm-NC、SSPL、Elastic License、CC-BY-NC 等一切带
  非商业/额外限制的「源可得非开源」许可；GPL / AGPL 等强 copyleft 会与 MIT 冲突，
  引入前必须先讨论。
- 新增 Python 包同样适用。当前**零第三方 Python 依赖**，优先用标准库维持这条。
- 合并前核一遍每个依赖的 `license` 字段（含传递依赖），不要只看 `npm install` 成功。

当前依赖（2026-11-15 实测 `tools/node_modules/**/package.json`）：

| 依赖 | 版本 | 许可 |
|---|---|---|
| `agent-browser` | 0.42.0 | Apache-2.0 |
| `agent-desktop` | 1.2.0 | Apache-2.0 |

`tools/node_modules/` 已进 `.gitignore`，本项目只分发自己的源码，因此 Apache-2.0 的
NOTICE 分发义务不触发；若将来打包分发依赖产物，需另附 THIRD_PARTY_NOTICES。

## 不要做的事

- 不加自动登录、不碰验证码、不读取或导出 token。
- 不点「去完成」类任务按钮，不自动消耗补登卡之外的付费资源。
- 不为了「看起来成功」放宽判据（历史上就这么踩过两次坑：签到线连着两天没真正执行却报成功）。

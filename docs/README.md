<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 文档索引

[主 README](../README.md) · [English README](../README.en.md) · [SKILL.md](../SKILL.md)

第一次用，按 **① 兼容性 → ② 安装 → ③ 用法** 的顺序看，其余按需查。

| 文档 | 什么时候看 | 你会得到什么 |
|---|---|---|
| [① 兼容性](compatibility.md) | **动手之前** | 版本红线（≥ 5.11）、为什么 5.9.x 不行、两条官方升级/下载路径、其余硬依赖清单 |
| [② 安装](install.md) | 新机器 / 换机器 | 可照抄的 5 步安装 + 每步的验证输出 |
| [③ 用法](usage.md) | 装完之后 | 命令表、五个任务与当日账本、退避与静默规则、开机自启 |
| [④ CDP 端口](ports.md) | `deps check` 报 CDP MISSING / 换过端口 | 端口自适应的四级来源、归属判定、`ports negotiate`、重启后不失效 |
| [⑤ 签到与补签判据](checkin-semantics.md) | 想搞清「到底算不算签上了」 | 权威数据源、翻日竞态、日历图例色值、补签的两道校验 |
| [⑥ 架构与安全边界](architecture.md) | 想改代码 / 评审安全性 | 链路图、Electron 侧的实测约束、模块职责、不做的事 |
| [⑦ 故障排查](troubleshooting.md) | 报错了 | 按现象查原因，跨机器排查顺序 |
| [⑧ 变更记录](changelog.md) | 想知道哪版改了什么 | 按日期的改动 + 每次踩过的坑 |

## 约定

- **时间口径**：一律北京时间（naive 时间按北京时间解释），账本次日 0 点翻页。
- **退出码**：`0` 正常；`1` 依赖缺失或运行失败；`2` 缺 `BUDDY_FREE_MODEL=1`；`3` 客户端版本低于红线。
- **路径**：账本与日志在 `~/.local/share/buddy-web-checkin/`，登录项在 `~/Library/LaunchAgents/`。
- 文档里的命令都在仓库根目录执行，`./run.sh` 是唯一入口。

## 文档语言与命名约定

主文档为简体中文（WorkBuddy 的主要受众），英文版只有一份总览 [README.en.md](../README.en.md)；
细节文档暂不翻译，Issue 用中文或英文都可以。

文件命名遵循 **BCP 47 语言标签 + 点号后缀**，这条是有意选的，不是随手写：

| 写法 | 判断 | 说明 |
|---|---|---|
| `README.en.md` ✅ | 采用 | 点号分隔、语言码小写、无地区。同款见 HelloGitHub（`README.md` + `README_en.md` 的中文项目） |
| `README_EN.md` ⚠️ | 不用 | 大写不算错但属历史风格（doocs/leetcode 这类）；`EN` 是**语言码**不是国家码，全大写容易让人误以为是 ISO 3166 |
| `README_zh_CN.md` / `README-zh_CN.md` ⚠️ | 需要地区时才用 | ant-design 用 `README-zh_CN.md`，996.ICU 用 `README_CN.md`（直接拿国家码当语言码）。真要标地区，规范写法是 `README.zh-CN.md`（语言-地区） |
| `README.en-US.md` | 不必 | 只有内容真按地区分叉（价格、合规、支付方式）时才加地区码；本项目的英文版没有地区差异 |

**加新翻译时**：`README.<ISO 639-1 小写语言码>.md`，例如 `README.ja.md`、`README.ko.md`；
只有当同一语言需要按地区分版本时才写成 `README.pt-BR.md`、`README.zh-TW.md`。
文档站的 i18n 目录同理用 `docs/<lang>/`（对齐 kubernetes/website 的 `content/zh-cn/`、`pt-br/` 全小写连字符风格）。

为什么不用 `_EN`：`EN` 是语言码，`US/CN/GB` 才是国家二字码（ISO 3166-1 alpha-2）。
把两者混成一个全大写后缀在中文项目里很常见，但既不符合 BCP 47，也让「英文」和「美式英语」
在文件名上无法区分 —— 我们只维护一份英文，用最小、最不容易误解的 `en` 就够。

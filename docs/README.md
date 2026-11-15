<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 文档索引

[主 README](../README.md) · [English README](../README_EN.md) · [SKILL.md](../SKILL.md) · [MIT LICENSE](../LICENSE)

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

主文档为简体中文（WorkBuddy 的主要受众），英文版只有一份总览 [README_EN.md](../README_EN.md)；
细节文档暂不翻译，Issue 用中文或英文都可以。

### 先说结论：分隔符没有国际标准

`_` / `.` / `-` 三种写法都有大量项目在用。**GitHub 只渲染 `README.md`，完全不识别语言后缀**，
语言切换靠的是两份 README 顶部手写的互链 —— 所以分隔符是「选一种、全仓一致」的约定，
不是合规问题。本仓库用 `README_EN.md`，是因为它属于根目录翻译文件里最常见的一派，
**没有任何技术理由说 `.` 更正确**。

2026-11-15 逐个查 GitHub 仓库根目录的真实命名：

| 仓库 | 翻译文件 | 分隔符 |
|---|---|---|
| 521xueweihan/HelloGitHub | `README_en.md` / `README_ja.md` | `_` |
| doocs/leetcode | `README_EN.md` | `_` |
| PaddlePaddle/Paddle | `README_cn.md` / `README_ja.md` | `_` |
| 996.ICU | `README_CN.md` | `_` |
| ant-design/ant-design | `README-zh_CN.md` | `-` |
| jwasham/coding-interview-university | `translations/README-zh.md` 等 | `-` + 目录 |
| kubernetes/website | `content/en/`、`content/zh-cn/`、`content/pt-br/` | 目录 |

可以看到：**根目录的翻译 README 里 `_` 明显占多数**（本仓库之前的 `README.en.md`
属于点号那一派，多见于多语言文档工具链与 Apache 内容协商后缀风格，没有功能优势）。
文档站层面则是 BCP 47 全小写连字符目录（`zh-cn`、`pt-br`）成主流。

### 真正有标准的是「语言码」，不是「分隔符」

| 概念 | 标准 | 写法 |
|---|---|---|
| 语言码 | ISO 639-1 | `en` / `zh` / `ja`（规范小写） |
| 国家 / 地区码 | ISO 3166-1 alpha-2 | `US` / `CN` / `TW`（规范大写） |
| 语言标签 | BCP 47（语言-地区） | `en-US` / `zh-CN` / `pt-BR` |

- `README_EN.md` 里的 `EN` 是**语言码全大写**，属于中文开源圈的显示习惯，
  不是国家码（国家码是 `US`），也不影响功能 —— 文件名不会被当语言标签解析。
- `README_CN.md` 是**拿国家码代替语言码**，严格讲不准确，但同样很常见。
- 只有内容真按地区分叉（价格、合规、支付方式）时才需要地区码；本项目英文版没有地区差异，
  所以不需要 `-US`。

### 加新翻译时

1. 沿用现有风格：`README_<语言码>.md`（例如 `README_ja.md`、`README_ko.md`，建议小写）。
2. 需要按地区分叉才写 `README_zh-TW.md`、`README_pt-BR.md`。
3. **不要混用分隔符**（不要再出现 `README.en.md` / `README-ja.md`），并在两份 README
   顶部保持互链。
4. 文档分册将来若做多语言，走 `docs/<lang>/`（对齐 Kubernetes 的 `zh-cn` / `pt-br` 风格），
   而不是再堆一个 `docs-zh/` 目录。

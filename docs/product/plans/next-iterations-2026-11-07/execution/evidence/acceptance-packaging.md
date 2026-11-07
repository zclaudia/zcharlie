# PK-01/02独立审查与包装验收

日期：2026-11-07。审查角色：Sol子代理，非本批作者。审查基线HEAD=`1f62fa4ac3dd22be5ef65550ae6ac0444098b313`及下列dirty文件内容；未覆盖simulation/061并行改动。本记录仅审查共享包装与BASE-03指定修正，结论不替代四功能逐阶段的实际资产验收。

结论：PK-01/02本卡退出条件通过；BASE-03的all-skipped拒绝及嵌套basetemp修正验证通过。未发现阻断或需回修的一般问题。业务文件只读，未替作者改代码。

## 审查范围与判断

- pyproject package-data仅增加`static/pages/*.css`；原模板、JS、顶层CSS、brand规则保留，未使用递归全目录打包，未升级依赖。
- `prepare_source`只复制pyproject和src到临时树，排除dotenv、pyc及egg-info；真实构建不在checkout内运行，不安装wheel，不触发依赖下载。现有backend缺失时仅选择本地已缓存且符合约束的纯Pythonbackend；缺缓存明确失败而非skip。
- `assert_wheel_assets`按精确ZIP member检查存在且非空，并拒绝dotenv/.git条目；空expected清单失败。manifest每个方向的文件名与执行任务卡相符；未来方向尚未存在的资产不会被本轮core成功误报为已打包。
- 包装测试在临时源码加入独立`static/pages/packaging_probe.css`探针，用实际setuptools wheel同时验证已有模板、JS、嵌套CSS；缺失asset与空检查反例均拒绝。
- wrapper仅执行显式allowlist；未实现scope/空清单拒绝，不扫描整仓。pytest早期plugin覆盖兄弟unit/integration；Python子进程保留guard。
- pytest_sessionfinish在返回码0但无passed报告时改成5，all-skipped不会成功；对应子进程反例确实触发5。嵌套pytest按PID选择不同basetemp，子进程结束后父fixture仍存在。
- Node分支要求真实声明测试及passed数量，排除空文件合成通过项；pass/empty/skip三例分别为0/5/5；bootstrap的禁外连和DOM支持用实际node:test子进程验证。

## 独立运行结果

实际运行cwd为项目根。两个入口独立建立临时sandbox；未读取真实账户/数据库或调用外部provider。

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `.venv/bin/python scripts/check_next_iterations.py --scope PK --offline` | 0 | 3 passed，3.97s |
| `.venv/bin/python scripts/check_next_iterations.py --scope BASE-03 --offline` | 0 | 19 passed，6.48s |

两次各出现2条PytestAssertRewriteWarning（logfire和next_iterations_offline已在启动期导入）。不影响运行结果；guard在collection前加载是设计要求，未观察到外连放行或测试跳过。

未验证：后续尚未创建的质量/收件箱/银行/压力/决定完整资产清单；已存在G0资产也不由本卡core探针替代G0阶段验收。未发布/安装wheel，未启动完整应用、浏览器、设备或生产服务；不作真实布局、生产兼容或真实E2E通过声明。

## 审查输入hash

| 文件 | SHA256 |
|---|---|
| `pyproject.toml` | `e13d0d59215aadf14d6020d654c1272f52017bbec137e7e7524eaf55e7f01ee7` |
| `tests/support/next_iterations_package.py` | `b5de131b5667e913dd0f5e9740779ed23ef52cd63605d4dfd7d48180fdb3ce47` |
| `tests/next_iterations/test_package_assets.py` | `e499942f2c2e43a6708254adcfc1925e880488c471d85ad0187d5e3f2e94faa6` |
| `tests/fixtures/next_iterations/package-assets.json` | `aab460bbb023da03d80a4e479775152e7d50e01367132bdb2b22492c221df739` |
| `scripts/check_next_iterations.py` | `1b5dd14b6a33cb539f89fed78ade9502cc1bf577064c1432483583940ad04c1a` |
| `tests/support/next_iterations_offline.py` | `a740db745e177990e151d8130aa5b50e2dce03789a03c76ab49007edbfa2221c` |
| `tests/next_iterations/test_offline_guard.py` | `24bf629ce47afccc3da9371e1d34d043f9c3079fee3ab0556dc0610168eeecd8` |

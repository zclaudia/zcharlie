# 卡 02:基线包 YAML 与加载

设计依据:§4.1、§11 M1「基线包 YAML」。前置:卡 01。

## 文件

1. 新建 `config/methods/company-research.v1.yaml`:卡 01 `LEGACY_BASELINE_DEFINITION` 的 YAML 形式
   (package_key: company-research;description 用中文简述「公司研究现行要求的显式化,v2 基线」;
   YAML 中不写 artifact_id/version/content_hash 等生命周期字段)。
2. `investor/method_packages.py` 增加:
   - `DEFAULT_METHODS_DIR = Path("config/methods")`
   - `load_package_file(path: Path) -> MethodPackageDefinition`:读 YAML → `parse_definition` →
     `validate_definition`(错误聚合为 `MethodPackageError`)。
   - `load_packages(directory: Path = DEFAULT_METHODS_DIR) -> tuple[MethodPackageDefinition, ...]`
     (目录内全部 `*.yaml`,按文件名排序)。

## 一致性测试(新增到 `tests/unit/test_method_packages.py`)

- `load_package_file` 读仓库内基线包成功,`canonical_hash` 等于 `canonical_hash(LEGACY_BASELINE_DEFINITION)`
  (description 不参与相等性:比较时将两者 description 置同值后断言哈希相等,或直接断言除 description 外
  canonical definition 相等——目的:YAML 与代码内基线不允许悄悄分叉)。
- 基线包不含超出 `ResearchProgress.depth` 现行要求的任何要求:断言 blocking 步骤恰为
  falsifiable_plan / dimension_assessment 两类,counter_analysis 为 non-blocking 且无 required_outputs,
  discipline 三开关全 False。

## 验收

三门禁 + 目标测试全绿;除新增文件外只允许改 `method_packages.py` 与其测试。

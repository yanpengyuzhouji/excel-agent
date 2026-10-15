"""Domain word tables extracted from DB-GPT parser.py.

The Chinese vocabulary (COMMON_COLUMN_NAMES / HEADER_FIELD_WORDS) is a
power-industry prior; it is harmless to keep and the review UI lets users
override the proposed physical names.  Move/replace here for easier customisation.
"""

# Common Chinese column headers -> preferred snake_case physical names.
COMMON_COLUMN_NAMES = {
    "序号": "sequence_no",
    "物料编码": "material_code",
    "固化id": "solidified_id",
    "物料描述": "material_description",
    "扩展说明": "extended_description",
    "计量单位": "unit",
    "单位": "unit",
    "数量": "quantity",
    "中类描述": "category_description",
    "备注": "remark",
    "应用场景": "application_scenario",
    "选用说明": "selection_note",
    "项目名称": "project_name",
    "专业类别": "discipline_category",
    "设计阶段": "design_stage",
    "项目编码": "project_code",
    "设计编号": "design_number",
    "图号": "drawing_number",
    "编制单位": "compilation_organization",
    "编制日期": "compilation_date",
}

# Cell values treated as "no data" placeholders.
PLACEHOLDER_VALUES = {"/", "-", "--", "n/a", "na", "无", "不适用"}

# Short noun labels that make a row look like a real header.
HEADER_FIELD_WORDS = {
    "大类",
    "中类",
    "小类",
    "实施范围",
    "建议采购方式",
    "建议采购方式1",
    "建议采购组织方式",
    "备注",
    "类别",
    "序号",
    "月份",
    "计划编号",
    "计划名称",
    "申报开始",
    "基线日期",
    "审查开始",
    "审查结束",
    "上报国网",
    "发布公告",
    "开标时间",
    "定标时间",
    "采购类别",
    "项目类型",
    "公告时间",
    "公示时间",
    "物料编码",
    "物料描述",
    "数量",
    "单位",
    "是否需要签订合同",
    "是否集中会签",
    "集中会签理由",
    "来源",
    "名称",
    "编码",
    "类型",
}

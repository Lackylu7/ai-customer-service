from dataclasses import dataclass
from typing import Literal


BusinessDomain = Literal[
    "hr",
    "shareholder",
    "activity",
    "finance",
    "operations",
    "unknown",
]
PolicyAction = Literal["allow", "clarify", "handoff", "deny"]
PolicyReason = Literal[
    "knowledge_gap",
    "sensitive_financial_data",
    "prohibited_financial_action",
    "tax_compliance",
    "unauthorized_financial_data",
    "pending_business_rule",
    "compliance_review",
    "financial_calculation",
    "controlled_financial_data",
    "sensitive_personal_data",
    "sensitive_credentials",
    "historical_or_unverified_rule",
    "missing_business_context",
    "prohibited_hr_action",
    "out_of_scope_topic",
    "out_of_scope_sensitive_topic",
    "system_unavailable",
]
ChatChannel = Literal["group", "direct"]


@dataclass(frozen=True)
class PolicyDecision:
    domain: BusinessDomain
    action: PolicyAction
    reason: PolicyReason | None = None


class EnterprisePolicy:
    _SALARY_TERMS = (
        "工资",
        "薪资",
        "课时费",
        "发薪",
        "房补",
        "住宿补贴",
        "社保",
        "公积金",
    )
    _PERSONAL_DATA_TERMS = (
        "身份证",
        "银行卡",
        "病历",
        "保单",
        "工资明细",
        "薪资明细",
        "扣款明细",
        "审批数据",
        "入住名单",
        "宿舍地址",
        "住址",
        "手机号",
        "电话号码",
    )
    _DISCLOSURE_TERMS = (
        "提供",
        "发给",
        "发到",
        "发出来",
        "列出",
        "查询",
        "查看",
        "展示",
        "公开",
        "给我",
        "所有",
        "名单",
        "明细",
        "直接发",
    )
    _STORE_CONTROLLED_FINANCIAL_TERMS = (
        "基本户账号",
        "法人登记证号",
        "租金",
        "管理费",
    )
    _STORE_PERSON_RELATION_TERMS = (
        "股东",
        "族长",
        "城市经理",
        "师傅股东",
        "技术股东",
    )
    _PERSON_IDENTITY_TERMS = (
        "是谁",
        "姓名",
        "名单",
        "哪个人",
        "哪些人",
        "负责人",
    )
    _CREDENTIAL_TERMS = (
        "密码",
        "口令",
        "wi-fi",
        "wifi",
        "内部账号",
    )
    _OUT_OF_SCOPE_SENSITIVE_TERMS = (
        "政治",
        "政党",
        "国家领导人",
        "评价政府",
        "评价国家",
        "习近平",
        "台独",
        "台湾独立",
        "六四",
        "天安门事件",
    )
    _OUT_OF_SCOPE_GENERAL_TERMS = (
        "天气",
        "天气预报",
        "气温",
        "会下雨",
        "下不下雨",
    )
    _DOMAIN_TERMS: tuple[tuple[BusinessDomain, tuple[str, ...]], ...] = (
        (
            "shareholder",
            (
                "股东",
                "入股",
                "分红",
                "持股",
                "退股",
                "门店实收",
                "消耗收入",
                "现金利润",
                "充值累计负债",
                "有赞数据",
                "回本",
                "风险金",
                "开分店",
                "开下一家",
                "开第二家",
                "下一家店",
            ),
        ),
        (
            "activity",
            (
                "活动",
                "活动卡",
                "68折",
                "会员日",
                "老带新",
                "老客奖励",
                "介绍朋友",
                "周年庆",
                "优惠",
                "卡肯定不过期",
            ),
        ),
        (
            "finance",
            (
                "报销",
                "发票",
                "退款",
                "退卡",
                "退多少钱",
                "财务",
                "备用金",
                "收款账户",
                "银行账号",
                "银行账户",
                "现金垫付",
                "出差",
                "伙食",
                "酒店标准",
                "商务舱",
                "差旅",
                "团建",
                "费用分类",
                "定金",
            ),
        ),
        (
            "operations",
            (
                "客户",
                "客诉",
                "投诉",
                "采购",
                "盘点",
                "跨门店",
                "线上开单",
                "无前台门店",
                "空位",
                "物料",
                "门店",
                "开业",
                "开业时间",
                "店型",
                "门店分级",
                "床位",
                "实体店",
                "管理口径",
                "门店状态",
                "待开业",
                "新店",
                "管理门店",
            ),
        ),
        (
            "hr",
            (
                "入职",
                "人事",
                "招聘",
                "合同",
                "考勤",
                "新教练",
                "约课",
                "星探课",
                "工服",
                "形象照",
                "大众点评入驻",
                "前台操作",
                "打卡",
                "调店",
                "调动",
                "转正",
                "离职",
                "新人",
                "教练",
                "在职证明",
                "工作证明",
                "实习证明",
                "住宿申请",
                "住宿",
                "宿舍",
                "有赞",
                "美众云",
                "新人培训",
            ),
        ),
    )

    def evaluate(
        self,
        question: str,
        *,
        channel: ChatChannel = "group",
    ) -> PolicyDecision:
        if channel not in ("group", "direct"):
            raise ValueError("channel must be 'group' or 'direct'.")
        normalized = "".join(question.lower().split())
        domain = self._route(normalized)
        if any(term in normalized for term in self._OUT_OF_SCOPE_GENERAL_TERMS):
            return PolicyDecision(
                domain="unknown",
                action="deny",
                reason="out_of_scope_topic",
            )
        if any(
            term in normalized for term in self._OUT_OF_SCOPE_SENSITIVE_TERMS
        ):
            return PolicyDecision(
                domain=domain,
                action="deny",
                reason="out_of_scope_sensitive_topic",
            )
        if any(term in normalized for term in self._CREDENTIAL_TERMS):
            return PolicyDecision(
                domain=domain,
                action="deny" if channel == "group" else "handoff",
                reason="sensitive_credentials",
            )
        if any(
            term in normalized for term in self._STORE_PERSON_RELATION_TERMS
        ) and any(term in normalized for term in self._PERSON_IDENTITY_TERMS):
            return PolicyDecision(
                domain=domain,
                action="deny" if channel == "group" else "handoff",
                reason="sensitive_personal_data",
            )
        if any(
            term in normalized for term in self._STORE_CONTROLLED_FINANCIAL_TERMS
        ) and any(term in normalized for term in self._DISCLOSURE_TERMS):
            return PolicyDecision(
                domain=domain if domain != "unknown" else "finance",
                action="deny" if channel == "group" else "handoff",
                reason="controlled_financial_data",
            )
        if any(term in normalized for term in self._PERSONAL_DATA_TERMS) and any(
            term in normalized for term in self._DISCLOSURE_TERMS
        ):
            return PolicyDecision(
                domain=domain,
                action="deny" if channel == "group" else "handoff",
                reason="sensitive_personal_data",
            )
        if any(
            term in normalized
            for term in ("历史", "旧版", "停用", "废止", "已失效", "尚未生效", "未生效")
        ) or (
            "新规则" in normalized
            and any(term in normalized for term in ("现在", "执行", "生效"))
        ):
            return PolicyDecision(
                domain=domain,
                action="handoff",
                reason="historical_or_unverified_rule",
            )
        if channel == "group" and any(
            term in normalized for term in ("银行账号", "银行账户", "收款账户")
        ):
            return PolicyDecision(
                domain=domain,
                action="deny",
                reason="sensitive_financial_data",
            )
        if channel == "direct" and any(
            term in normalized for term in ("银行账号", "银行账户", "收款账户")
        ):
            return PolicyDecision(
                domain=domain,
                action="handoff",
                reason="controlled_financial_data",
            )
        if any(
            term in normalized
            for term in ("批准", "审批", "审批通过", "直接同意")
        ) and any(
            term in normalized for term in ("报销", "退款", "转账", "付款", "入股")
        ):
            return PolicyDecision(
                domain=domain,
                action="deny",
                reason="prohibited_financial_action",
            )
        if any(
            term in normalized
            for term in (
                "分多少钱",
                "能分多少",
                "分红多少",
                "分红金额",
                "能拿多少分红",
            )
        ):
            return PolicyDecision(
                domain="shareholder",
                action="handoff",
                reason="financial_calculation",
            )
        if any(term in normalized for term in ("抵税", "税务合规", "发票合法")):
            return PolicyDecision(
                domain=domain,
                action="handoff",
                reason="tax_compliance",
            )
        if any(term in normalized for term in ("其他门店", "其他人", "他人")) and any(
            term in normalized for term in ("报销", "分红", "退款", "工资", "薪资")
        ):
            return PolicyDecision(
                domain=domain,
                action="deny",
                reason="unauthorized_financial_data",
            )
        if self._is_pending_shareholder_rule(normalized):
            return PolicyDecision(
                domain="shareholder",
                action="handoff",
                reason="pending_business_rule",
            )
        if (
            "退款" in normalized
            and "2000" in normalized
            and "收款码" in normalized
            and "银行卡" in normalized
        ):
            return PolicyDecision(
                domain="finance",
                action="handoff",
                reason="pending_business_rule",
            )
        if (
            "总部实习生" in normalized
            and "住宿" in normalized
            and any(term in normalized for term in ("多少", "费用", "收费"))
        ):
            return PolicyDecision(
                domain="hr",
                action="handoff",
                reason="pending_business_rule",
            )
        if any(term in normalized for term in ("直接", "自动", "替我")) and any(
            term in normalized
            for term in ("转正", "解除", "离职", "盖章", "处罚", "罚款")
        ):
            return PolicyDecision(
                domain=domain,
                action="deny",
                reason="prohibited_hr_action",
            )
        if (
            "老带新" in normalized
            or "老客奖励" in normalized
            or ("介绍朋友" in normalized and "奖励" in normalized)
        ) or (
            "68折" in normalized
            and "会员日" in normalized
            and any(term in normalized for term in ("同时", "叠加", "既"))
        ):
            return PolicyDecision(
                domain="activity",
                action="handoff",
                reason="pending_business_rule",
            )
        if any(term in normalized for term in ("没发票", "没有发票", "开不了发票")) and any(
            term in normalized for term in ("顶上", "替票", "其他发票", "别的发票")
        ):
            return PolicyDecision(
                domain="finance",
                action="handoff",
                reason="compliance_review",
            )
        if "退多少钱" in normalized or (
            "退款" in normalized
            and any(term in normalized for term in ("金额", "计算", "扣多少"))
        ):
            return PolicyDecision(
                domain=domain if domain != "unknown" else "finance",
                action="handoff",
                reason="financial_calculation",
            )
        if "课时费" in normalized and not any(
            city in normalized for city in ("深圳", "上海")
        ):
            return PolicyDecision(
                domain="finance",
                action="clarify",
                reason="missing_business_context",
            )
        if domain != "unknown":
            return PolicyDecision(domain=domain, action="allow")
        # Unknown wording is not itself unsafe. Let the knowledge-answering
        # layer understand natural language and decide whether evidence is
        # sufficient; confirmed high-risk requests have already been stopped
        # above. This avoids turning the keyword router into an FAQ allowlist.
        return PolicyDecision(domain="unknown", action="allow")

    def _route(self, normalized: str) -> BusinessDomain:
        # 报销属于财务的强意图；即使问题同时提到分红等跨域数据，
        # 也先由财务权限边界处理，避免落到股东知识问答。
        if "报销" in normalized:
            return "finance"
        if self._is_colloquial_dividend_timing(normalized):
            return "shareholder"
        if self._is_colloquial_store_qualification(normalized):
            return "shareholder"
        if self._is_colloquial_shareholder_underperformance(normalized):
            return "shareholder"
        if self._is_shareholder_report_labor_cost(normalized):
            return "shareholder"
        if any(term in normalized for term in ("合同", "约课账号", "约课系统")):
            return "hr"
        if any(term in normalized for term in self._SALARY_TERMS) or any(
            term in normalized for term in ("意外险", "保险理赔", "报案")
        ):
            return "finance"
        for domain, terms in self._DOMAIN_TERMS:
            if any(term in normalized for term in terms):
                return domain
        return "unknown"

    @staticmethod
    def _is_colloquial_dividend_timing(normalized: str) -> bool:
        if "分期" in normalized:
            return False
        transfer_terms = (
            "转了钱",
            "转钱",
            "钱转过去",
            "打了款",
            "打款后",
        )
        timing_terms = (
            "开始分",
            "啥时候分",
            "什么时候分",
            "几月分",
            "哪个月分",
        )
        return any(term in normalized for term in transfer_terms) and any(
            term in normalized for term in timing_terms
        )

    @staticmethod
    def _is_shareholder_report_labor_cost(normalized: str) -> bool:
        return (
            any(term in normalized for term in ("财务报表", "损益表", "报表"))
            and "人工成本" in normalized
            and any(term in normalized for term in ("薪资", "工资", "教练", "前台"))
        )

    @staticmethod
    def _is_colloquial_store_qualification(normalized: str) -> bool:
        return any(
            term in normalized
            for term in (
                "再开一家",
                "开下一家",
                "下一家店",
                "开第二家",
                "再开一店",
            )
        )

    @staticmethod
    def _is_colloquial_shareholder_underperformance(normalized: str) -> bool:
        return (
            "股东" in normalized
            and any(term in normalized for term in ("连续", "几个月"))
            and any(
                term in normalized
                for term in ("没做到要求", "没达标", "不达标", "未达标", "低于要求")
            )
        )

    @staticmethod
    def _is_pending_shareholder_rule(normalized: str) -> bool:
        if EnterprisePolicy._is_colloquial_shareholder_underperformance(normalized):
            return True
        checks = (
            (
                ("入股", "确认"),
                ("多久", "超时", "失败", "时间限制", "几天"),
            ),
            (
                ("分红表",),
                ("什么时候", "多久", "几号", "时间"),
            ),
            (
                ("有赞", "财务报表"),
                ("差异", "不一样", "不同"),
            ),
            (
                ("增持",),
                ("回本", "40%", "什么时候", "条件", "多久", "7日"),
            ),
            (
                ("技术股东", "退股"),
                ("折旧", "怎么算", "计算", "几年"),
            ),
            (
                ("注资",),
                (
                    "不同类型",
                    "技术股东",
                    "师傅股东",
                    "开店主体",
                    "门店股东",
                    "什么时候",
                    "多久",
                ),
            ),
            (
                ("业绩",),
                ("不达标", "没达标", "未达标", "低于标准", "不够"),
            ),
        )
        return any(
            all(term in normalized for term in required)
            and any(term in normalized for term in qualifiers)
            for required, qualifiers in checks
        )

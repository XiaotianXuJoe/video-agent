"""智能下一步提示：规则引擎从状态机确定性派生，禁止 LLM 生成动作。
每个动作 = {label, action, payload?}，前端点击后以 __action: 消息回发。"""


def chips_for(project: dict, pipeline_state: dict) -> list:
    if not project:
        return [
            {"label": "制作算法讲解视频", "action": "start_chat",
             "hint": "发给我题目和正确代码，我们开始"},
            {"label": "制作科普讲解视频", "action": "start_chat",
             "hint": "告诉我主题和受众"},
        ]

    status = pipeline_state.get("status", "created")
    gate = pipeline_state.get("gate")
    tier = pipeline_state.get("tier")
    stage = pipeline_state.get("stage", 1)

    if status == "awaiting_gate" and gate:
        g = gate["stage"]
        chips = []
        if g == 3:
            chips.append({"label": "确认创意，生成分镜", "action": "confirm_gate"})
            chips.append({"label": "换个创意方向", "action": "redo_stage", "stage": 3})
            chips.append({"label": "直接做到成片", "action": "upgrade_tier", "tier": "video"})
        elif g == 5:
            chips.append({"label": "确认分镜，继续生成视频", "action": "upgrade_tier", "tier": "video"})
            chips.append({"label": "修改分镜稿", "action": "free_text",
                          "hint": "直接输入对分镜的修改意见，如「第3镜例子换成…」"})
            chips.append({"label": "重新生成分镜", "action": "redo_stage", "stage": 4})
        elif g == 7:
            chips.append({"label": "关键帧可以，继续配音渲染", "action": "confirm_gate"})
            chips.append({"label": "标注修改画面", "action": "open_artifact", "artifact": "keyframes"})
            chips.append({"label": "重新生成视觉代码", "action": "redo_stage", "stage": 6})
        elif g == 11:
            chips.append({"label": "审片通过，完成", "action": "finish"})
            chips.append({"label": "标注修改视频", "action": "open_artifact", "artifact": "video"})
            chips.append({"label": "重新配音", "action": "redo_stage", "stage": 8})
        return chips

    if status == "tier_done":
        if tier == "concept":
            return [
                {"label": "继续生成分镜", "action": "upgrade_tier", "tier": "storyboard"},
                {"label": "直接做到成片", "action": "upgrade_tier", "tier": "video"},
                {"label": "换个创意方向", "action": "redo_stage", "stage": 3},
            ]
        if tier == "storyboard":
            return [
                {"label": "继续生成视频", "action": "upgrade_tier", "tier": "video"},
                {"label": "修改分镜稿", "action": "free_text",
                 "hint": "直接输入对分镜的修改意见"},
                {"label": "重新生成分镜", "action": "redo_stage", "stage": 4},
            ]
        return [
            {"label": "标注修改视频", "action": "open_artifact", "artifact": "video"},
            {"label": "重新配音（换音色/语速）", "action": "redo_stage", "stage": 8},
            {"label": "查看 QC 报告", "action": "open_artifact", "artifact": "qc"},
        ]

    if status == "failed":
        return [
            {"label": "查看失败详情", "action": "show_error"},
            {"label": "重试当前阶段", "action": "retry_stage"},
            {"label": "去设置页检查模型配置", "action": "open_settings"},
        ]

    if status == "running":
        return [{"label": "查看流水线进度", "action": "show_pipeline"}]

    if status == "paused":
        return [
            {"label": "继续执行后续阶段", "action": "resume_pipeline"},
            {"label": "重跑当前阶段", "action": "retry_stage"},
            {"label": "查看流水线面板（可点击任意节点执行）", "action": "show_pipeline"},
        ]

    if status == "finished":
        return [
            {"label": "标注修改视频", "action": "open_artifact", "artifact": "video"},
            {"label": "查看 QC 报告", "action": "open_artifact", "artifact": "qc"},
            {"label": "再做一个视频", "action": "new_chat"},
        ]

    return [
        {"label": "生成创意方案", "action": "start_tier", "tier": "concept"},
        {"label": "直接生成分镜", "action": "start_tier", "tier": "storyboard"},
        {"label": "直接生成视频", "action": "start_tier", "tier": "video"},
    ]

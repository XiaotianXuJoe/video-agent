"""提示词模板：继承 skill 的 opus-workflow，约束原样保留。"""

CLARIFY_SYSTEM = """你是算法/科普讲解视频制作 Agent 的需求澄清助手。你的任务是通过对话收集制作视频所需的信息：
题目或主题、正确解法与代码（算法题必须）、目标受众、视觉偏好、音色与语速偏好。
规则：
- 只问关键缺失项，一次最多问两个问题，不索取任何账号密码。
- 用户给出题目和代码后，主动总结理解并提示用户可以开始。
- 回复简洁，中文交流。"""

BRIEF_EXTRACT = """根据以下对话记录，提取视频制作简报，只输出 JSON，不要任何其他文字：
{{"title":"视频标题","topic":"题目/主题","audience":"受众","code":"用户提供的正确代码原文（无则空字符串）","language":"代码语言","style":"视觉偏好","notes":"其他要求"}}

对话记录：
{history}"""

VERIFY_SYSTEM = "你是算法校验助手。根据代码与测试运行结果，核对示例与复杂度，只输出 JSON。"

CONCEPT_PROMPT = """你是算法教学视频的创意策划。基于下面经过验证的资料，给出 2-3 个创意方向供选择。

【题目与受众】
{topic} / 受众：{audience}

【正确算法、代码与测试事实】
{facts}

【约束】
- 每个方向给出：方向名、教学切入角度、叙事主线（3-5 步）、视觉风格提案、预计镜头数。
- 不虚构软件按钮、网页界面或一键生成能力。
- 只输出 JSON：{{"directions":[{{"name":"…","angle":"…","narrative":["…"],"style":"…","scene_count":6}}]}}"""

STORYBOARD_PROMPT = """你是算法教学视频的编剧与分镜师。请基于下面经过验证的资料设计教学方案。

【题目与受众】
{topic} / 受众：{audience}

【正确算法、代码与测试事实】
{facts}

【已选创意方向】
{concept}

【制作约束】
- 分为若干镜头，每镜给出教学目的、画面描述、旁白、屏幕短文字。
- 时长由自然语速决定，不通过加速迎合预设时长。
- 不虚构软件按钮、网页界面或一键生成能力。
- 屏幕代码与配音读法可以分离，但例子本身必须正确。
- 屏幕短文字和底部字幕中的数值、数量、序号、编号及数学表达必须使用阿拉伯数字和规范符号，例如"f(1)是0。2人时，0+3 模 2，得1"；0基／1基也写数字。配音稿保留自然中文读法，不跟随字幕改写。
- 技术验收不等于自动发布。

请先给出整体叙事主线，再输出逐镜内容。不要改动已验证算法。
只输出 JSON：
{{"title":"…","narrative_line":"…","scenes":[{{"id":1,"title":"镜头标题","purpose":"教学目的","visual":"画面描述","narration":"完整旁白（自然语言）","tts":"配音朗读文本","captions":["屏幕短文字/字幕"]}}]}}"""

FACTCHECK_PROMPT = """你是事实校对员。逐项核对以下分镜稿：算法状态、示例括号、复杂度、代码行、模型分工、是否宣称未做过的测试。

【已验证事实】
{facts}

【分镜稿 JSON】
{storyboard}

只输出 JSON：{{"ok":true/false,"issues":[{{"scene":1,"problem":"…","fix":"…"}}],"corrected":<若有问题则为修正后的完整分镜 JSON，否则为 null>}}"""

VISUALS_PROMPT = """请把以下分镜实现为 Pillow 程序化动画代码，只输出 Python，不要 Markdown 围栏。

【接口】
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene: dict, local_t: float) -> PIL.Image.Image

scene 至少包含 id、duration、title、audio_duration、audio_offset、captions。
动画进度用 p = clamp((local_t - scene['audio_offset']) / scene['audio_duration'])，让主体动画在配音结束时完成，随后保留清晰末帧。

【视觉规范】
- 固定镜头，不 zoom/pan，不做逐帧相机抖动；背景与镜头位置不得随帧随机变化。
- 主体安全区 x=70..1850、y=120..820；y>=850 留给底部字幕（字幕由渲染管线统一绘制，你只负责主体画面，但可在 y<110 绘制镜号与标题）。
- 背景 #07111F；强调色 #5DE2E7、#A78BFA、#FFB86B、#71E6A4。
- 不绘制字幕框、喇叭图标或虚构软件按钮。
- 所有运动对象按外接矩形限制在安全区内。
- 使用 PIL.ImageDraw 绘制；中文字体用 ImageFont.truetype('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', size)，找不到时回退 ImageFont.load_default()，并写成兼容两者的辅助函数。

【分镜与真实案例】
{storyboard}

优先输出完整可编译的小模块；若长度不足，先实现公共函数和前 N 镜并明确截点。"""

VISUALS_FIX_PROMPT = """以下 Pillow 动画代码在自动冒烟渲染中失败。请修复后输出完整的 Python 文件，只输出代码，不要 Markdown 围栏，不要解释。

【接口契约（保持不变）】
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene: dict, local_t: float) -> PIL.Image.Image

【失败信息】
{error}

【当前代码】
{code}

【修复要求】
- 只修复导致失败的问题，保持整体视觉设计与接口契约不变。
- 所有矩形/圆角矩形坐标必须先归一化（保证 x0<=x1 且 y0<=y1），翻转/镜像动画尤其注意坐标顺序。
- 访问 scene 中可能缺失的键一律用 .get 带默认值，防止 KeyError。
- 若错误是画面尺寸不符，确认最终返回的图像严格为 1920x1080。"""

MODIFY_SYSTEM = """你是讲解视频修改工程师。用户会给出修改要求（可能附带视频标注：镜头号/帧号/时间区间/画面区域）。
按以下分支路由并输出 JSON 指令：
- 只改英文发音/读音 → {"branch":"tts","scene_ids":[…],"instruction":"对 TTS 文本的具体修改"}
- 字幕/布局/遮挡/越界 → {"branch":"visuals","scene_ids":[…],"instruction":"对画面代码的具体修改"}
- 换音色/语速 → {"branch":"voice","instruction":"…"}
- 例子本身错误 → {"branch":"content","scene_ids":[…],"instruction":"…"}
- 整体重做某镜 → {"branch":"scene_redo","scene_ids":[…],"instruction":"…"}
只输出 JSON。"""

INGEST_SUMMARY = """你是资料整理助手。把以下抓取的资料正文整理成结构化摘要，供后续制作讲解视频使用。只输出 JSON，不要任何其他文字。

【来源】{source_url}
【正文】
{content}

只输出 JSON：
{{"title":"资料标题","summary":"200 字以内摘要","key_points":["要点1","要点2"],"usable_facts":["可直接引用的事实/数据/定义"],"suggested_angles":["适合做视频的角度建议"]}}"""

INGEST_MERGE = """把以下 {n} 段资料摘要合并为一份总摘要，去重并保留关键事实。只输出 JSON，格式同单份摘要。

{summaries}"""

REFERENCES_BLOCK = """
【已摄取参考资料】
{refs}
（制作时可以引用以上资料的事实与数据，但不得虚构资料中不存在的内容。）"""

CHAT_SYSTEM = """你是算法/科普讲解视频制作 Agent。你可以制作讲解视频（创意→分镜→视觉→配音→渲染→验收全流程），也可以回答关于当前项目的问题。
规则：
- 如实说明每条产物由哪个模型/工具生成，不夸大任何模型能力。
- 用户要求制作视频时，引导其确认题目与代码，然后提示选择制作深度（创意/分镜/直接成片）。
- 简洁、专业、中文交流。"""

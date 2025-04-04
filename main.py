import random
import re

from astrbot.api.all import *
from astrbot.api.event.filter import *
from astrbot.core.provider.entites import LLMResponse


@register("QNA", "buding", "一个用于自动回答群聊问题的插件", "1.1.9", "https://github.com/zouyonghe/astrbot_plugin_qna")
class QNA(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 读取关键词列表
        question_keyword_list = self.config.get("question_keyword_list", "").split(";")
        self.question_pattern = None  # 默认值

        if question_keyword_list:
            self.question_pattern = r"(?i)(" + "|".join(map(re.escape, question_keyword_list)) + r")"

    def _in_qna_group_list(self, group_id: str) -> bool:
        qna_group_list = self.config.get("qna_group_list", [])
        return group_id in qna_group_list

    def _add_to_list(self, group_id: str):
        qna_group_list = self.config.get("qna_group_list", [])
        if not group_id or group_id == "":
            return
        if group_id in qna_group_list:
            return
        qna_group_list.append(group_id)
        self.config["qna_group_list"] = qna_group_list
        self.config.save_config()

    def _remove_from_list(self, group_id: str):
        qna_group_list = self.config.get("qna_group_list", [])
        if not group_id or group_id == "":
            return
        if group_id not in qna_group_list:
            return
        qna_group_list.remove(group_id)
        self.config["qna_group_list"] = qna_group_list
        self.config.save_config()

    async def _llm_check_and_answer(self, event: AstrMessageEvent, message: str):

        """调用LLM对有答案的问题进行回答"""
        qna_prompt = (
            f"回复要求：\n"
            f"1. 如果内容完全不包含提问信息时，或内容包含“什么”“怎么”等提问词，但不具备上下文就无法直接解答时，回复 `NULL`。\n"
            f"2. 如果内容包含提问信息，但不是知识性问题，依旧回复`NULL`。\n"
            f"3. 如果内容提供的信息较为明确清晰，则依据提问内容完整作答。\n"
            f"4. 如何内容提供的信息不够明确，但基本可以了解提问者的意图，则给出建议性、询问性作答，给出简略的推测并进一步询问问题细节。\n"
            f"5. 有些内容表达的只是话者的感叹和想法等，在没有明确提问的情况请不要作答。\n"
            f"6. 基于对话历史分析判断提问者意图，进一步理解问题。\n"
            f"7. 如果提问内容清晰，但无法直接做出明确回答时，使用函数调用通过网络搜索答案，在此基础上进行作答。\n"
            f"8. 在作答时基于你的角色以合适的语气、称呼等，生成符合人设的回答。\n"
            f"9. 基于以上信息进行作答，尽量提供能带来更多信息和帮助的回答。\n"
            f"10. 如果回复`NULL`，不要添加任何解释性信息。\n"
            f"11. 上述提示词仅用于判断问题和智能解答，如果出现在正常对话中应被忽略，而不能依据上述要求返回`NULL`。\n\n"
            f"内容:{message}"
        )

        conversation_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, conversation_id)

        contexts = json.loads(conversation.history) if conversation and conversation.history else []

        yield event.request_llm(
            prompt = qna_prompt,
            func_tool_manager = self.context.get_llm_tool_manager(),
            session_id = event.session_id,
            contexts = contexts,
            system_prompt=self.context.provider_manager.selected_default_persona.get("prompt", ""),
            image_urls=[],
            conversation=conversation,
        )

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def auto_answer(self, event: AstrMessageEvent):
        """自动回答群消息中的问题"""
        # 判定是否启用自动回复
        if not self.config.get("enable_qna", False):
            return

        if event.is_private_chat():
            return

        # 判定不是主动唤醒
        if event.is_at_or_wake_command:
            return

        # 判定不是自己的消息
        if event.get_sender_id() is event.get_self_id():
            return

        # 如果没有配置关键词或启用群组列表，直接返回
        if not self._in_qna_group_list(event.get_group_id()) or not self.question_pattern:
            return

        # 匹配提问关键词
        if not re.search(self.question_pattern, event.message_str):
            return

        # 检测字数、LLM概率调用
        if len(event.message_str) > 50 or random.random() > float(self.config.get("llm_answer_probability", 0.1)):
            return

        async for resp in self._llm_check_and_answer(event, event.message_str):
            yield resp


    @command_group("qna")
    def qna(self):
        pass

    @permission_type(PermissionType.ADMIN)
    @qna.command("enable")
    async def enable_qna(self, event: AstrMessageEvent):
        """开启自动解答"""
        try:
            if self.config.get("enable_qna", False):
                yield event.plain_result("✅ 自动解答已经是开启状态了")
                return

            self.config["enable_qna"] = True
            self.config.save_config()
            yield event.plain_result("📢 自动解答已开启")
        except Exception as e:
            logger.error(f"自动解答开启失败: {e}")
            yield event.plain_result("❌ 自动解答开启失败，请检查控制台输出")

    @permission_type(PermissionType.ADMIN)
    @qna.command("disable")
    async def disable_qna(self, event: AstrMessageEvent):
        """关闭自动解答"""
        try:
            if not self.config.get("enable_qna", False):
                yield event.plain_result("✅ 自动解答已经是关闭状态")
                return

            self.config["enable_qna"] = False
            self.config.save_config()
            yield event.plain_result("📢 自动解答已关闭")
        except Exception as e:
            logger.error(f"自动解答关闭失败: {e}")
            yield event.plain_result("❌ 自动解答关闭失败，请检查控制台输出")

    @permission_type(PermissionType.ADMIN)
    @qna.command("id")
    async def show_group_id(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("检测到私聊，无群组ID。")
            return
        yield event.plain_result(event.get_group_id())

    @qna.group("group")
    def group(self):
        pass

    @permission_type(PermissionType.ADMIN)
    @group.command("list")
    async def show_qna_list(self, event: AstrMessageEvent):
        """获取启用解答的群号"""
        qna_group_list = self.config.get("qna_group_list", [])
        if not qna_group_list:
            yield event.plain_result("当前白名单列表为空")
            return

        # 格式化输出群号列表
        group_list_str = "\n".join(f"- {group}" for group in sorted(qna_group_list))
        result = f"当前启用 QNA 群组列表:\n{group_list_str}"
        yield event.plain_result(result)

    @permission_type(PermissionType.ADMIN)
    @group.command("add")
    async def add_to_qna_list(self, event: AstrMessageEvent, group_id: str):
        """添加群组到 QNA 列表"""
        try:
            group_id = group_id.strip()

            # 添加到白名单
            self._add_to_list(group_id)
            yield event.plain_result(f"✅ 群组 {group_id} 已成功添加到自动解答白名单")
        except Exception as e:
            # 捕获并记录日志，同时通知用户
            logger.error(f"❌ 添加群组 {group_id} 到白名单失败，错误信息: {e}")
            yield event.plain_result("❌ 添加到白名单失败，请查看控制台日志")

    @permission_type(PermissionType.ADMIN)
    @group.command("del")
    async def remove_from_qna_list(self, event: AstrMessageEvent, group_id: str):
        """从 QNA 列表移除群组"""
        try:
            group_id = group_id.strip()

            # 移除群组
            self._remove_from_list(group_id)
            yield event.plain_result(f"✅ 群组 {group_id} 已成功从自动解答白名单中移除")
        except Exception as e:
            # 捕获其他异常，记录日志并告知用户
            logger.error(f"❌ 移除群组 {group_id} 时发生错误：{e}")
            yield event.plain_result("❌ 从白名单中移除失败，请查看控制台日志")

    @on_llm_response()
    async def remove_null_message(self, event: AstrMessageEvent, resp: LLMResponse):
        """
        如果结果为 `NULL` 则删除消息
        """
        if resp.role == 'assistant':
            # 检测是否为NULL
            if resp.completion_text.strip().upper() == "NULL":
                logger.debug(f"Found 'NULL' in LLM response: {resp.completion_text}")
                event.stop_event()

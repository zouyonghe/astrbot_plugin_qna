import logging
import random
import re

from astrbot.api.all import *
from astrbot.core.provider.entites import ProviderRequest
from astrbot.core.utils.metrics import Metric

logger = logging.getLogger("astrbot")


@register("QNA", "buding", "一个用于自动回答群聊问题的插件", "0.0.1", "https://github.com/zouyonghe/astrbot_plugin_qna")
class QNA(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # 读取关键词列表
        question_keyword_list = self.config.get("question_keyword_list", "").split(";")
        self.question_pattern = None  # 默认值

        if question_keyword_list:
            self.question_pattern = r"(?i)(" + "|".join(map(re.escape, question_keyword_list)) + r")"
            logger.debug(f"自动问答关键词正则: {self.question_pattern}")

    def _in_qna_group_list(self, event: AstrMessageEvent) -> bool:
        qna_group_list = self.config.get("qna_group_list", "").split(";")
        if str(event.get_group_id()) in qna_group_list:
            logger.debug(f"群 {event.get_group_id()} 在自动回答名单内")
            return True
        return False

    async def _llm_check_and_answer(self, event: AstrMessageEvent, message: str):
        logger.error("HERE0 called!")

        """调用 LLM 判断并回复，只有在信息字数 < 50 并且满足概率要求时才执行"""
        llm_probability = float(self.config.get("llm_answer_probability", 0.1))
        if len(message) > 50 or random.random() > llm_probability:
            return
        logger.error("HERE called!")
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("No available LLM provider")
            return
        logger.error("HERE 2 called!")

        """调用LLM对有答案的问题进行回答"""
        qna_prompt = (
            f"回复要求：\n"
            f"1. 如果内容不包含提问信息，回复 `NULL`。\n"
            f"2. 如果内容包含提问关键字，如“什么”“怎么”等，但不具备上下文，无法直接解答，回复 `NULL`。\n"
            f"3. 如果输入是明确的且可解答的疑问句，则基于系统提示词生成合适的回答。\n"
            f"4. 请根据上述规则判断，尽量对能够回答的问题作答。\n"
            f"5. 如果回复`NULL`，则不要附加任何额外解释信息。\n\n"
            f"提问内容:{message}"
        )

        try:
            req = ProviderRequest(prompt=qna_prompt, image_urls=[])

            conversation_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            logger.error(f"conversation_id: {conversation_id}")
            logger.error(f"sender_id: {event.get_sender_id()}")

            if not conversation_id:
                conversation_id = await self.context.conversation_manager.new_conversation(event.unified_msg_origin)
            req.session_id = conversation_id
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, conversation_id)
            req.conversation = conversation
            req.contexts = json.loads(conversation.history)

            logger.error(f"request: {req.__dict__}")

            # qna_response = await provider.text_chat(
            #     prompt=qna_prompt,
            #     session_id=str(event.get_sender_id()),
            #     system_prompt=req.system_prompt,
            #     contexts=req.contexts
            # )
            qna_response = await provider.text_chat(**req.__dict__)

            logger.error(f"answer {qna_response.completion_text}")
            if qna_response and qna_response.completion_text:
                answer = qna_response.completion_text
                if answer.strip() == "NULL":
                    return
                yield event.plain_result(answer)

            contexts = req.contexts
            new_record = {
                "role": "user",
                "content": req.prompt
            }
            contexts.append(new_record)
            contexts.append({
                "role": "assistant",
                "content": qna_response.completion_text
            })
            contexts_to_save = list(filter(lambda item: '_no_save' not in item, contexts))

            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                conversation_id,
                contexts_to_save
            )
            await Metric.upload(
                llm_tick=1,
                model_name=provider.get_model(),
                provider_type=provider.meta().type
            )

        except Exception as e:
            logger.error(f"在调用LLM回复时报错: {e}")

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def auto_answer(self, event: AstrMessageEvent):
        """自动回答群消息中的问题"""
        # 判定是否启用自动回复
        if not self.config.get("enable_qna", False):
            return

        # 如果没有配置关键词或启用群组列表，直接返回
        if not self.question_pattern or not self._in_qna_group_list(event):
            return

        # 遍历消息，匹配关键词
        for comp in event.get_messages():
            if isinstance(comp, BaseMessageComponent):
                message = comp.toString().strip()
                if re.search(self.question_pattern, message):
                    async for resp in self._llm_check_and_answer(event, message):
                        yield resp




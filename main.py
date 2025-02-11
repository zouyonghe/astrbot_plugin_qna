import inspect
import logging
import random
import re
from typing import AsyncGenerator, Awaitable

from astrbot.api.all import *
from astrbot.core import astrbot_config
from astrbot.core.provider.entites import ProviderRequest
from packages.astrbot.long_term_memory import LongTermMemory
from packages.astrbot.main import Main

logger = logging.getLogger("astrbot")


@register("QNA", "buding", "一个用于自动回答群聊问题的插件", "1.0.0", "https://github.com/zouyonghe/astrbot_plugin_qna")
class QNA(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.ltm = None
        self.bot = None
        self.bot_wake_prefix = tuple(p for p in astrbot_config['wake_prefix'] if p)
        self.LLM_wake_prefix = astrbot_config['provider_settings']['wake_prefix']

        if self.context.get_config()['provider_ltm_settings']['group_icl_enable'] or self.context.get_config()['provider_ltm_settings']['active_reply']['enable']:
            try:
                self.ltm = LongTermMemory(self.context.get_config()['provider_ltm_settings'], self.context)
            except BaseException as e:
                logger.error(f"聊天增强 err: {e}")

        # 读取关键词列表
        question_keyword_list = self.config.get("question_keyword_list", "").split(";")
        self.question_pattern = None  # 默认值

        if question_keyword_list:
            self.question_pattern = r"(?i)(" + "|".join(map(re.escape, question_keyword_list)) + r")"

    def _in_qna_group_list(self, event: AstrMessageEvent) -> bool:
        qna_group_list = [
            group.strip() for group in self.config.get("qna_group_list", "").split(";")
            if group.strip() and not group.startswith("#")
        ]
        if str(event.get_group_id()) in qna_group_list:
            return True
        return False

    def _load_star(self):
        if self.bot is None:
            main = self.context.get_registered_star(star_name="astrbot").star_cls
            if isinstance(main, Main):
                self.bot = main

    async def _llm_check_and_answer(self, event: AstrMessageEvent, message: str):

        """调用 LLM 判断并回复，只有在信息字数 < 50 并且满足概率要求时才执行"""
        llm_probability = float(self.config.get("llm_answer_probability", 0.1))
        if len(message) > 50 or random.random() > llm_probability:
            return

        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("No available LLM provider")
            return

        """调用LLM对有答案的问题进行回答"""
        qna_prompt = (
            f"回复要求：\n"
            f"1. 如果内容完全不包含提问信息时，或内容包含“什么”“怎么”等提问词，但不具备上下文就无法直接解答时，回复 `NULL`。\n"
            f"2. 如果内容包含提问信息，但不是知识性问题，依旧回复`NULL`。\n"
            f"3. 如果内容提供的信息较为明确清晰，则依据提问内容完整作答。\n"
            f"4. 如何内容提供的信息不够明确，但基本可以了解提问者的意图，则给出建议性、询问性作答，给出简略的推测并进一步询问问题细节。\n"
            f"5. 有些内容表达的只是话者的感叹和想法等，在没有明确提问的情况请不要作答。\n"
            f"6. 基于对话历史分析判断提问者意图，进一步理解问题。\n"
            f"7. 对于提问内容清晰，但无法明确回答的问题，可以通过函数调用通过网络搜索答案。\n"
            f"8. 在作答时基于你的角色以合适的语气、称呼等，生成符合人设的回答。\n"
            f"9. 基于以上信息进行作答，尽量提供能带来更多信息和帮助的回答。\n"
            f"10. 如果回复`NULL`，则必须以`NULL`开头，在其后添加空格并添加简略的不回复原因，不要添加任何额外内容。\n\n"
            f"内容:{message}"
        )

        try:
            req = ProviderRequest(prompt=qna_prompt, image_urls=[])
            req.session_id = event.session_id

            conversation_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not conversation_id:
                conversation_id = await self.context.conversation_manager.new_conversation(event.unified_msg_origin)
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, conversation_id)
            req.conversation = conversation
            req.contexts = json.loads(conversation.history)
            req.system_prompt = self.context.provider_manager.selected_default_persona.get("prompt", "")
            req.func_tool = self.context.get_llm_tool_manager()

            if isinstance(req.contexts, str):
                req.contexts = json.loads(req.contexts)

            await self.bot.decorate_llm_req(event, req)

            logger.error(f"REQUEST: {str(req)}")

            qna_response = await provider.text_chat(**req.__dict__)

            if qna_response.role == 'assistant':
                answer = qna_response.completion_text
                logger.error(f"ANSWER_1: {str(answer)}")
                if answer.strip().startswith("NULL"):
                    return
                yield event.plain_result(answer)
            elif qna_response.role == 'err':
                event.plain_result(f"AstrBot 请求失败。\n错误信息: {qna_response.completion_text}")
            elif qna_response.role == 'tool':
                # function calling
                function_calling_result = {}
                for func_tool_name, func_tool_args in zip(qna_response.tools_call_name, qna_response.tools_call_args):
                    func_tool = req.func_tool.get_func(func_tool_name)
                    logger.info(f"调用工具函数：{func_tool_name}，参数：{func_tool_args}")
                    try:
                        # 尝试调用工具函数
                        wrapper = self._call_handler(event, func_tool.handler, **func_tool_args)
                        async for resp in wrapper:
                            if resp is not None:
                                function_calling_result[func_tool_name] = resp
                            else:
                                yield
                        event.clear_result()  # 清除上一个 handler 的结果
                    except Exception as e:
                        logger.error(f"LLM函数调用异常: {str(e)}")
                        function_calling_result[func_tool_name] = "When calling the function, an error occurred: " + str(e)

                if function_calling_result:
                    logger.error(f"RESULT: {str(function_calling_result)}")
                    extra_prompt = "\n\nSystem executed some external tools for this task and here are the results:\n"
                    for tool_name, tool_result in function_calling_result.items():
                        extra_prompt += f"Tool: {tool_name}\nTool Result: {tool_result}\n"
                else:
                    extra_prompt = "\n\nSystem executed some external tools for this task but NO results found.\n"

                req.prompt += extra_prompt
                qna_response = await provider.text_chat(**req.__dict__)

                if qna_response.role == 'assistant':
                    answer = qna_response.completion_text
                    logger.error(f"ANSWER_2x: {str(answer)}")
                    if answer.strip().startswith("NULL"):
                        return
                    yield event.plain_result(answer)
                elif qna_response.role == 'err':
                    event.plain_result(f"AstrBot 请求失败。\n错误信息: {qna_response.completion_text}")
                elif qna_response.role == 'tool':
                    logger.debug("QNA不支持循环函数调用")
                    return

            await self.bot.after_llm_req(event)
        except Exception as e:
            logger.error(f"在调用LLM回复时报错: {e}")

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def auto_answer(self, event: AstrMessageEvent):
        """自动回答群消息中的问题"""
        # 获取main实例
        self._load_star()

        # 判定是否启用自动回复
        if not self.config.get("enable_qna", False):
            return

        # 如果没有配置关键词或启用群组列表，直接返回
        if not self.question_pattern or not self._in_qna_group_list(event):
            return

        # 检测到两类唤醒词均交给原始流程处理
        if self.bot_wake_prefix and event.message_str.startswith(self.bot_wake_prefix):
            return

        if self.LLM_wake_prefix and event.message_str.startswith(self.LLM_wake_prefix):
            return

        if re.search(self.question_pattern, event.message_str):
            async for resp in self._llm_check_and_answer(event, event.message_str):
                yield resp

        # # 遍历消息，匹配关键词
        # for comp in event.get_messages():
        #     if isinstance(comp, BaseMessageComponent):
        #         message = comp.toString().strip()
        #         logger.error(f"message: {message}")
        #         if re.search(self.question_pattern, message):
        #             async for resp in self._llm_check_and_answer(event, message):
        #                 yield resp

    async def _call_handler(
            self,
            event: AstrMessageEvent,
            handler: Awaitable,
            **params
    ) -> AsyncGenerator[None, None]:
        '''调用 Handler。'''
        # 判断 handler 是否是类方法（通过装饰器注册的没有 __self__ 属性）
        ready_to_call = handler(event, **params)

        if isinstance(ready_to_call, AsyncGenerator):
            async for ret in ready_to_call:
                # 如果处理函数是生成器，返回值只能是 MessageEventResult 或者 None（无返回值）
                if isinstance(ret, (MessageEventResult, CommandResult)):
                    event.set_result(ret)
                    yield
                else:
                    yield ret
        elif inspect.iscoroutine(ready_to_call):
            # 如果只是一个 coroutine
            ret = await ready_to_call
            if isinstance(ret, (MessageEventResult, CommandResult)):
                event.set_result(ret)
                yield
            else:
                yield ret


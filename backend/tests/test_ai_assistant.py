import unittest
from unittest.mock import Mock, patch
import requests
from app.ai_native import chat, _clean_result, _tool_result
from app.ai_capabilities import validate_action

class AssistantTests(unittest.TestCase):
    def test_new_tools_navigation(self):
        for tool in ('json', 'pdf-toolbox'):
            result = _tool_result({'tool_calls': [{'function': {'name': 'navigate_tool', 'arguments': {'tool_id': tool}}}]})
            self.assertEqual(result['navigate_to'], tool)
            with self.assertRaises(ValueError): validate_action('create', tool, {})
    def test_invalid_action(self):
        self.assertIsNone(_clean_result({'action': {'operation': 'create', 'tool_id': 'invented'}}, 'home')['action'])
    def test_chat(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'tool_calls': [{'function': {'name': 'navigate_tool', 'arguments': '{"tool_id":"pdf-toolbox"}'}}]}}]}
        with patch('app.ai_native.resolve_model', return_value=('url', 'secret', 'model')), patch('app.ai_native.requests.post', return_value=response):
            self.assertEqual(chat({'messages': [{'role': 'user', 'content': '打开 PDF 工具箱'}]})['navigate_to'], 'pdf-toolbox')
    def test_errors(self):
        for status, phrase in ((401, '密钥'), (403, '权限'), (404, '接口'), (429, '限流'), (503, '请求失败')):
            response = requests.Response(); response.status_code = status
            with patch('app.ai_native.resolve_model', return_value=('url', 'secret', 'model')), patch('app.ai_native.requests.post', side_effect=requests.HTTPError('SECRET', response=response)):
                with self.assertRaises(ValueError) as caught: chat({})
                self.assertIn(phrase, str(caught.exception)); self.assertNotIn('SECRET', str(caught.exception))
    def test_timeout(self):
        with patch('app.ai_native.resolve_model', return_value=('url', 'secret', 'model')), patch('app.ai_native.requests.post', side_effect=requests.Timeout()):
            with self.assertRaisesRegex(ValueError, '超时'): chat({})

    def test_pdf_answer_is_replanned(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'tool_calls': [{'function': {'name': 'respond_to_user', 'arguments': {'answer': '无法转换'}}}]}}]}
        plan = _tool_result({'tool_calls': [{'function': {'name': 'create_task', 'arguments': {'tool_id': 'pdf', 'parameters': {'urls': ['https://baidu.com']}}}}]})
        with patch('app.ai_native.resolve_model', return_value=('url','key','model')), patch('app.ai_native.requests.post', return_value=response), patch('app.ai_native._request_route_decision', return_value=True), patch('app.ai_native._request_structured_plan', return_value=plan):
            result = chat({'messages': [{'role':'user','content':'将 https://baidu.com 转PDF'}]})
        self.assertEqual(result['action']['tool_id'], 'pdf')
        self.assertEqual(result['action']['parameters']['urls'], ['https://baidu.com'])
        self.assertTrue(result['action']['requires_confirmation'])

    def test_agent_observes_query_before_answer(self):
        query = _clean_result({'action': {'operation':'query','tool_id':'pdf','parameters':{}}}, 'home')
        executor = Mock(return_value={'message':'已完成','tasks':[{'id':'task-12345678','status':'completed'}]})
        with patch('app.ai_native._plan_chat', side_effect=[query, _clean_result({'answer':'任务已完成'},'home')]) as planner:
            result=chat({'messages':[], '_observations':[{'forged':True}]}, executor)
        self.assertEqual(len(result['observations']),1)
        self.assertEqual(result['answer'],'任务已完成')
        self.assertFalse(executor.call_args.args[0]['confirmed'])

    def test_agent_never_auto_executes_creation(self):
        plan=_clean_result({'action':{'operation':'create','tool_id':'pdf','parameters':{'urls':['https://example.com']}}},'home')
        executor=Mock()
        with patch('app.ai_native._plan_chat',return_value=plan):
            result=chat({},executor)
        executor.assert_not_called()
        self.assertTrue(result['action']['requires_confirmation'])

    def test_agent_stops_repeated_query(self):
        plan=_clean_result({'action':{'operation':'query','tool_id':'pdf','parameters':{}}},'home')
        executor=Mock(return_value={'message':'处理中'})
        with patch('app.ai_native._plan_chat',return_value=plan):
            result=chat({},executor)
        self.assertEqual(executor.call_count,1)
        self.assertIsNone(result['action'])

    def test_live_events_precede_execution_and_result(self):
        events=[]
        query=_clean_result({'action':{'operation':'query','tool_id':'pdf','parameters':{}}},'home')
        def execute(payload):
            self.assertEqual(events[-1]['label'],'调用工具')
            return {'message':'已找到任务'}
        with patch('app.ai_native._plan_chat', side_effect=[query,_clean_result({'answer':'已找到任务'},'home')]):
            chat({},execute,events.append)
        self.assertEqual([event['label'] for event in events],['规划下一步','调用工具','收到结果','规划下一步','生成答复'])
        self.assertEqual(events[1]['action']['tool_id'],'pdf')

    def test_tool_dump_forces_plan_without_route_guess(self):
        response=Mock()
        response.json.return_value={'choices':[{'message':{'content':'pdf\n{"urls":["https://baidu.com"]}'}}]}
        plan=_tool_result({'tool_calls':[{'function':{'name':'create_task','arguments':{'tool_id':'pdf','parameters':{'urls':['https://baidu.com']}}}}]})
        with patch('app.ai_native.resolve_model',return_value=('url','key','model')), patch('app.ai_native.requests.post',return_value=response), patch('app.ai_native._request_route_decision') as route, patch('app.ai_native._request_structured_plan',return_value=plan):
            result=chat({'messages':[{'role':'user','content':'转PDF'}]})
        route.assert_not_called()
        self.assertEqual(result['action']['tool_id'],'pdf')
        self.assertNotIn('"urls"',result['answer'])

    def test_unknown_function_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'未知函数'):
            _tool_result({'tool_calls':[{'function':{'name':'pdf','arguments':{}}}]})

    def test_generic_page_action_is_validated_and_confirmed(self):
        for tool in ('json','pdf-toolbox','database','word-batch','image-convert'):
            action=validate_action('page',tool,{'revision':'snapshot-1','control_id':'3','mode':'fill','value':'example'})
            self.assertTrue(action['requires_confirmation'])
        with self.assertRaises(ValueError):
            validate_action('page','json',{'revision':'1','control_id':'2','mode':'script','value':'alert(1)'})
        with self.assertRaises(ValueError):
            validate_action('page','json',{'control_id':'2','mode':'click'})

    def test_json_conversion_format_is_preserved(self):
        result=_tool_result({'tool_calls':[{'function':{'name':'configure_tool','arguments':{'tool_id':'json','parameters':{'text':'[{"a":1}]','mode':'convert','format':'csv'}}}}]})
        self.assertEqual(result['config_patch']['values']['format'],'csv')
        with self.assertRaises(ValueError):
            validate_action('configure','json',{'mode':'convert','format':'unsupported'})

    def test_all_user_phrasings_use_model_planner(self):
        prompts=['将 https://baidu.com 转为PDF','对 {"name":"ls"} 进行格式化','对 {name: ls} 进行格式化','不要转换，解释一下','Format this JSON: [1,2]']
        for text in prompts:
            for with_executor in (False, True):
                with self.subTest(text=text, executor=with_executor):
                    payload={'messages':[{'role':'user','content':text}]}
                    with patch('app.ai_native._plan_chat', return_value={'answer':'模型答复'}) as planner:
                        result=chat(payload, Mock() if with_executor else None)
                    planner.assert_called_once()
                    self.assertEqual(planner.call_args.args[0]['messages'],payload['messages'])
                    self.assertEqual(result['answer'],'模型答复')

    def test_observations_reach_answer_route(self):
        response=Mock()
        response.json.return_value={'choices':[{'message':{'tool_calls':[{'function':{'name':'respond_to_user','arguments':{'answer':'任务已完成'}}}]}}]}
        observations=[{'result':{'tasks':[{'status':'completed'}]}}]
        with patch('app.ai_native.resolve_model',return_value=('url','key','model')), patch('app.ai_native.requests.post',return_value=response), patch('app.ai_native._request_route_decision',return_value=False) as route:
            chat({'messages':[{'role':'user','content':'查一下进度'}],'_observations':observations})
        self.assertEqual(route.call_args.args[-1], observations)

    def test_required_operation_cannot_downgrade_on_final_retry(self):
        response=Mock()
        response.json.return_value={'choices':[{'message':{'content':'工具参数未生成'}}]}
        with patch('app.ai_native.resolve_model',return_value=('url','key','model')), patch('app.ai_native.requests.post',return_value=response), patch('app.ai_native._request_route_decision',return_value=True), patch('app.ai_native._request_structured_plan',side_effect=ValueError('invalid')) as planner:
            with self.assertRaisesRegex(ValueError,'没有创建或执行任务'):
                chat({'messages':[{'role':'user','content':'执行这个任务'}]})
        self.assertTrue(all(call.kwargs.get('require_function') for call in planner.call_args_list))

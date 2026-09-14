#!/usr/bin/env python
# -*- coding: UTF-8 -*-
__doc__ = """
==============================================================================================================
Nebula数据导入导出脚本（Nebula 3.x）
--------------------------------------------------------------------------------------------------------------
1. 安装依赖包：pip install pandas nebula3-python==3.3.0 -i https://pypi.tuna.tsinghua.edu.cn/simple
2. 查看脚本执行说明：python nebula_data_util.py -h
3. 执行脚本
例：
python nebula_data_util.py export -n 101.200.139.185 -s RulesMap event_graph -d /Users/shunli/Desktop/nebula
python nebula_data_util.py import -n 127.0.0.1 -d /Users/shunli/Desktop/nebula
==============================================================================================================
"""

import os
import ast
import time

try:
    import pandas as pd

    from nebula3.gclient.net import ConnectionPool
    from nebula3.Config import Config
except:
    print("请安装依赖包：pip install pandas nebula3-python==3.3.0 -i https://pypi.tuna.tsinghua.edu.cn/simple")
    exit(1)


class SessionWrapper:
    _session = None

    def __init__(self, session, space=None):
        print("SessionWrapper初始化")
        self._session = session
        self.space = space
        if space:
            self._session.execute(f"use {self.space};")

    def do_execute(self, cmd):
        """执行命令"""

        query_resp = self._session.execute(cmd)
        print("do execute: %s" % cmd, query_resp.is_succeeded())
        if not query_resp.is_succeeded():
            print('Execute failed: %s' % query_resp.error_msg())
            raise Exception("服务异常")
        return query_resp

    def do_execute_json(self, cmd):
        """执行命令"""

        print("do execute: %s" % cmd)
        query_resp = self.do_execute(cmd)
        if not query_resp.is_succeeded():
            print('Execute failed: %s' % query_resp.error_msg())
            raise Exception("服务异常")
        return query_resp

    def release(self):
        self._session.release()

    def check_space(self, space):
        """检查space是否存在"""
        query_resp = self.do_execute_json(f"SHOW SPACES;")
        for name in query_resp.column_values('Name'):
            if name.as_string() == space:
                return True
        return False

    def chose_space(self, space):
        """选中space"""
        for i in range(5):
            try:
                self.do_execute(f"USE {space};")
                break
            except Exception as e:
                if i >= 4:
                    raise Exception("服务异常")
                print(e)
            time.sleep(5)


class NebulaSessionPool:

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

        self.connection_pool = ConnectionPool()
        Config.max_connection_pool_size = 20
        assert self.connection_pool.init([(self.host, self.port)], Config)

    def get_session(self, space=None):
        return SessionWrapper(self.connection_pool.get_session(self.user, self.password), space=space)

    def close(self):
        self.connection_pool.close()

    def __del__(self):
        print("断开连接...............")
        self.connection_pool.close()


class NebulaDataUtil:
    """Nebula数据导入导出工具类"""

    def __init__(self, host: str, port: int, user: str = "root", password: str = "nebula"):
        self.session_pool = NebulaSessionPool(host=host, port=port, user=user, password=password)

    @staticmethod
    def format_tags_info(data_info: pd.DataFrame, tag_props: str, prop_values: str):
        """
        格式化tags信息
        :param data_info:
        :param tag_props: 'type (images)'
        :param prop_values: '(11)'
        :return:
        """

        def format_tag_props(_dict: dict):
            return f"{_dict.get('type')} ({', '.join([k for k, v in _dict.items() if k != 'type' and v])})"

        def format_prop_values(_id: str, _list: list):
            values = ", ".join(['"{v}"'.format(v=v) for _dict in _list for k, v in _dict.items() if k != 'type' and v])
            return f"""{v_id}: ({values})"""

        for index, row in data_info.iterrows():
            v_id = row["v_id"]
            tags_info = row["tags_info"].replace("__NULL__", "''")
            tags_info = ast.literal_eval(tags_info)
            if not tag_props:
                tag_props = ", ".join([format_tag_props(info) for info in tags_info])

            if prop_values: prop_values += ", "
            prop_values += format_prop_values(v_id, tags_info)
            # prop_values += ", ".join([format_prop_values(v_id, info) for info in tags_info])

        return tag_props, prop_values

    @staticmethod
    def format_edges_info(data_info: pd.DataFrame, edge_type: str, edge_values: str):
        """
        格式化edges信息
        :param data_info:
        :param edge_type:
        :param edge_values:
        :return:
        """

        def format_edge_type(_dict: dict):
            return f"{_type} ({', '.join([k for k, v in _dict.items() if v])})"

        def format_edge_values(_dict: dict):
            return f"""{s_vid}->{d_vid}@{rank}:({', '.join(['"{v}"'.format(v=v) for k, v in _dict.items() if v])})"""

        for index, row in data_info.iterrows():
            s_vid = row["s_vid"]
            d_vid = row["d_vid"]
            rank = row["rank"]
            _type = row["type"]
            type_info = row["type_info"].replace("__NULL__", "''")
            type_info = ast.literal_eval(type_info)
            if not edge_type:
                edge_type = format_edge_type(type_info)

            if edge_values: edge_values += ", "
            edge_values += format_edge_values(type_info)

        return edge_type, edge_values

    @staticmethod
    def value_deal(col):
        """对象转python类型"""

        if col.is_empty():
            return None
        elif col.is_null():
            return None
        elif col.is_bool():
            return col.as_bool()
        elif col.is_int():
            return col.as_int()
        elif col.is_double():
            return col.as_double()
        elif col.is_string():
            return col.as_string()
        elif col.is_time():
            return col.as_time()
        elif col.is_date():
            return col.as_date()
        elif col.is_datetime():
            return col.as_datetime()
        elif col.is_list():
            return col.as_list()
        elif col.is_set():
            return col.as_set()
        elif col.is_map():
            return col.as_map()
        elif col.is_vertex():
            return col.as_node()
        elif col.is_edge():
            return col.as_relationship()
        elif col.is_path():
            return col.as_path()
        else:
            return ""

    def export_base_data(self, session: SessionWrapper, space_name: str, base_path: str):
        base_file = os.path.join(base_path, f"{space_name}_base.txt")
        with open(base_file, "w", encoding="utf-8") as f:
            resp = session.do_execute(f"SHOW CREATE SPACE {space_name};")
            data_list = [self.value_deal(col) for record in resp for col in record]
            f.write(data_list[-1].split(" ON ")[0].replace(", atomic_edge = false", "").replace("\n", "") + ";\n")

            tags_res = session.do_execute("SHOW TAGS;")
            tags = [self.value_deal(col) for record in tags_res for col in record]
            for tag in tags:
                tag_res = session.do_execute(f"SHOW CREATE TAG {tag};")
                tag_data = [self.value_deal(col) for record in tag_res for col in record]
                f.write(tag_data[-1].replace("\n", "") + ";\n")

            edges_res = session.do_execute("SHOW EDGES;")
            edges = [self.value_deal(col) for record in edges_res for col in record]
            for edge in edges:
                edge_res = session.do_execute(f"SHOW CREATE EDGE {edge};")
                edge_data = [self.value_deal(col) for record in edge_res for col in record]
                f.write(edge_data[-1].replace("\n", "") + ";\n")

    def export_node_data(self, session: SessionWrapper, space_name: str, base_path: str, max_size: int = 10000):
        node_data = {
            "v_id": [],
            "tags": [],
            "tags_info": []
        }
        resp = session.do_execute(f"MATCH (v) RETURN v limit {max_size};")
        data_list = [self.value_deal(col) for record in resp for col in record]
        for data in data_list:
            node_data["v_id"].append(data.get_id())
            node_data["tags"].append(",".join(data.tags()))
            node_data["tags_info"].append([data.properties(tag) for tag in data.tags()])

        pd.DataFrame(node_data).to_csv(os.path.join(base_path, f"{space_name}_node.csv"), index=False)

    def export_edge_data(self, session: SessionWrapper, space_name: str, base_path: str, max_size: int = 10000):
        node_data = {
            "s_vid": [],
            "d_vid": [],
            "rank": [],
            "type": [],
            "type_info": []
        }
        resp = session.do_execute(f"MATCH ()-[e]->() RETURN e limit {max_size};")
        data_list = [self.value_deal(col) for record in resp for col in record]
        for data in data_list:
            node_data["s_vid"].append(data.start_vertex_id())
            node_data["d_vid"].append(data.end_vertex_id())
            node_data["rank"].append(data.ranking())
            node_data["type"].append(data.edge_name())
            node_data["type_info"].append(data.properties())

        pd.DataFrame(node_data).to_csv(f"{base_path}/{space_name}_edge.csv", index=False)

    def import_base_data(self, session: SessionWrapper, base_path: str):
        for name in os.listdir(base_path):
            if name.endswith("_base.txt"):
                space_name = name.rsplit("_", maxsplit=1)[0]
                file_path = os.path.join(base_path, name)

                with open(file_path, "r") as f:
                    for i, line in enumerate(f.readlines()):
                        if i == 0 and not session.check_space(space_name):
                            session.do_execute(line)
                            time.sleep(5)
                            session.chose_space(space_name)
                        elif i > 0:
                            session.do_execute(line)

    def import_node_data(self, session: SessionWrapper, base_path: str, chunk_size: int = 100):
        for name in os.listdir(base_path):
            if name.endswith("_node.csv"):
                space_name = name.rsplit("_", maxsplit=1)[0]
                if session.space != space_name:
                    session.chose_space(space_name)
                file_path = os.path.join(base_path, name)
                node_data = pd.read_csv(file_path)

                for group_name, group_df in node_data.groupby("tags"):
                    tag_props, prop_values = "", ""
                    for i in range(0, len(group_df), chunk_size):
                        chunk = group_df.iloc[i:i + chunk_size]
                        tag_props, prop_values = self.format_tags_info(chunk, tag_props, prop_values)

                    session.do_execute(f"INSERT VERTEX {tag_props} VALUES {prop_values};")
                    # print(f"INSERT VERTEX {tag_props} VALUES {prop_values};")

    def import_edge_data(self, session: SessionWrapper, base_path: str, chunk_size: int = 100):
        for name in os.listdir(base_path):
            if name.endswith("_edge.csv"):
                space_name = name.rsplit("_", maxsplit=1)[0]
                if session.space != space_name:
                    session.chose_space(space_name)
                file_path = os.path.join(base_path, name)
                edge_data = pd.read_csv(file_path)

                for group_name, group_df in edge_data.groupby("type"):
                    edge_type, edge_values = "", ""
                    for i in range(0, len(group_df), chunk_size):
                        chunk = group_df.iloc[i:i + chunk_size]
                        edge_type, edge_values = self.format_edges_info(chunk, edge_type, edge_values)
                    session.do_execute(f"INSERT EDGE {edge_type} VALUES {edge_values};")
                    # print(f"INSERT EDGE {edge_type} VALUES {edge_values};")

    def export_nebula(self, space_names: list, base_path: str, max_size: int = 10000):
        """
        导出nebula数据
        """
        for space_name in space_names:
            session = self.session_pool.get_session(space=space_name)
            space_data_path = os.path.join(base_path, space_name)
            if not os.path.exists(space_data_path):
                os.makedirs(space_data_path)

            self.export_base_data(session, space_name, space_data_path)
            self.export_node_data(session, space_name, space_data_path, max_size)
            self.export_edge_data(session, space_name, space_data_path, max_size)
            session.release()

    def import_nebula(self, base_path: str, chunk_size: int = 100):
        """
        导入nebula数据
        """
        for space_name in os.listdir(base_path):
            if not space_name.startswith("."):
                session = self.session_pool.get_session(space=space_name)
                space_data_path = os.path.join(base_path, space_name)
                self.import_base_data(session, space_data_path)
                time.sleep(10)
                if not session.check_space(space_name):
                    session = self.session_pool.get_session(space=space_name)
                self.import_node_data(session, space_data_path, chunk_size)
                self.import_edge_data(session, space_data_path, chunk_size)
                session.release()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", help='子命令帮助')
    import_nebula = subparsers.add_parser('import', help='导入')
    import_nebula.add_argument(
        '-n', '--nebula_host', type=str, default="127.0.0.1", required=False,
        help="Nebula Host, default is 127.0.0.1"
    )
    import_nebula.add_argument(
        '-p', '--nebula_port', type=int, default=9669, required=False,
        help="Nebula Port, default is 9669"
    )
    import_nebula.add_argument(
        '-u', '--nebula_user', type=str, default="root", required=False,
        help="Nebula User, default is root"
    )
    import_nebula.add_argument(
        '-w', '--nebula_password', type=str, default="nebula", required=False,
        help="Nebula Password, default is nebula"
    )
    import_nebula.add_argument(
        '-z', '--size', type=int, default=100, required=False,
        help="批量导入，每次插入数, default is 100"
    )
    import_nebula.add_argument('-d', '--dst_dir', type=str, help="数据文件路径")

    export_nebula = subparsers.add_parser('export', help='导出')
    export_nebula.add_argument(
        '-n', '--nebula_host', type=str, default="127.0.0.1", required=False,
        help="Nebula Host, default is 127.0.0.1"
    )
    export_nebula.add_argument(
        '-p', '--nebula_port', type=int, default=9669, required=False,
        help="Nebula Port, default is 9669"
    )
    export_nebula.add_argument(
        '-u', '--nebula_user', type=str, default="root", required=False,
        help="Nebula User, default is root"
    )
    export_nebula.add_argument(
        '-w', '--nebula_password', type=str, default="nebula", required=False,
        help="Nebula Password, default is nebula"
    )
    export_nebula.add_argument(
        '-z', '--size', type=int, default=10000, required=False,
        help="最大导出个数, default is 10000"
    )
    export_nebula.add_argument('-s', '--spaces', nargs='+', type=str, help="指定导出的图空间")
    export_nebula.add_argument('-d', '--dst_dir', type=str, help="导出目录")

    args = parser.parse_args()

    nebula_obj = NebulaDataUtil(
        host=args.nebula_host, port=args.nebula_port, user=args.nebula_user, password=args.nebula_password
    )
    if args.command == 'import':
        nebula_obj.import_nebula(base_path=args.dst_dir, chunk_size=args.size)
    elif args.command == 'export':
        nebula_obj.export_nebula(space_names=args.spaces, base_path=args.dst_dir, max_size=args.size)
    else:
        raise ValueError('未知的子命令')

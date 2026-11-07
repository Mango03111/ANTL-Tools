"""
vLLM 并发性能压测脚本 (test_bench.py)

用途：
    用于测试大模型推理服务（如 vLLM 本地部署）在多用户并发场景下的吞吐量与延迟。
    模拟真实科研场景下，多个用户同时向服务端发送长文本请求时的排队情况。

运行环境：
    需安装 requests 库 (pip install requests)
    确保目标服务（vLLM）已在本地或指定 IP 的 8000 端口启动。

使用方法：
    直接运行：python3 test_bench.py
    或自定义并发数：python3 test_bench.py --concurrency 16
"""

import concurrent.futures
import requests
import time
import argparse

# ================= 配置区 =================
# 如果服务部署在其他机器上，请将 127.0.0.1 替换为目标 IP
SERVER_URL = "http://127.0.0.1:8000/v1/completions"
MODEL_NAME = "qwen38-27b-3bit"
HEADERS = {"Content-Type": "application/json"}
# ==========================================


def send_single_request(request_id: int) -> float:
    """
    发送单条测试请求
    :param request_id: 请求编号，用于日志追踪
    :return: 该请求的耗时（秒），如果失败则返回 -1
    """
    # 构造长文本 Prompt，模拟论文上下文的长度（约 2000 tokens）
    long_prompt = "请帮我总结这篇论文的核心贡献：Here is a long research paper about AI infrastructure and LLM serving. " * 100
    
    payload = {
        "model": MODEL_NAME,
        "prompt": long_prompt,
        "max_tokens": 128,
        "temperature": 0.0  # 设置为 0 保证输出确定性，方便性能对比
    }

    start_time = time.time()
    try:
        # 发送同步 POST 请求，超时设为 120 秒防止死锁
        response = requests.post(SERVER_URL, json=payload, headers=HEADERS, timeout=120)
        
        if response.status_code == 200:
            return time.time() - start_time
        else:
            print(f"[User {request_id}] 请求失败，状态码: {response.status_code}, 报错: {response.text}")
            return -1.0
            
    except Exception as e:
        print(f"[User {request_id}] 请求异常: {e}")
        return -1.0


def run_benchmark(concurrency: int = 8):
    """
    执行并发压测
    :param concurrency: 并发用户数量
    """
    print(f"===== 开始 {concurrency} 并发压测 =====")
    print(f"目标服务: {SERVER_URL}")
    print(f"模型名称: {MODEL_NAME}")
    
    start_time = time.time()
    
    # 使用线程池模拟并发请求
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        # 提交任务
        futures = [executor.submit(send_single_request, i) for i in range(concurrency)]
        # 收集结果
        results = [f.result() for f in futures]
    
    total_time = time.time() - start_time
    valid_times = [t for t in results if t > 0]
    
    print("\n===== 压测结果 =====")
    print(f"总并发数: {concurrency}")
    print(f"整体总耗时: {total_time:.2f} 秒")
    
    if valid_times:
        avg_time = sum(valid_times) / len(valid_times)
        max_time = max(valid_times)
        min_time = min(valid_times)
        print(f"单次请求平均耗时: {avg_time:.2f} 秒")
        print(f"单次请求最大耗时: {max_time:.2f} 秒 (排队最久的用户)")
        print(f"单次请求最小耗时: {min_time:.2f} 秒")
        
        # 粗略计算吞吐量 (QPS)
        qps = concurrency / total_time
        print(f"系统吞吐量 (QPS): {qps:.2f} req/s")
    else:
        print("所有请求均失败，请检查服务端是否正常运行。")


if __name__ == "__main__":
    # 支持通过命令行参数传入并发数，默认值为 8
    parser = argparse.ArgumentParser(description="vLLM 并发压测脚本")
    parser.add_argument("--concurrency", type=int, default=8, help="并发用户数量 (默认: 8)")
    args = parser.parse_args()
    
    run_benchmark(args.concurrency)

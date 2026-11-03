import concurrent.futures
import requests
import time

URL = "http://127.0.0.1:8000/v1/completions"
HEADERS = {"Content-Type": "application/json"}

def send_request(i):
    payload = {
        "model": "qwen38-27b-3bit",
        "prompt": "请帮我总结这篇论文的核心贡献：Here is a long research paper about AI infrastructure and LLM serving. " * 100,
        "max_tokens": 128,
        "temperature": 0.0
    }
    start = time.time()
    try:
        res = requests.post(URL, json=payload, headers=HEADERS, timeout=120)
        if res.status_code == 200:
            return time.time() - start
        else:
            return f"Error {res.status_code}: {res.text}"
    except Exception as e:
        return f"Exception: {e}"

def run_benchmark(concurrency=8):
    print(f"开始 {concurrency} 并发测试...")
    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(send_request, range(concurrency)))
    
    total_time = time.time() - start_time
    valid_times = [r for r in results if isinstance(r, float)]
    
    print(f"总耗时: {total_time:.2f} 秒")
    if valid_times:
        print(f"单次请求平均耗时: {sum(valid_times)/len(valid_times):.2f} 秒")
        print(f"单次请求最大耗时: {max(valid_times):.2f} 秒")
    
run_benchmark(8)
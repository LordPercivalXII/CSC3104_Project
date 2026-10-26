
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Main Smart AI Gateway endpoint
URL = "http://127.0.0.1:8000/chat"

# 6 requests: A = 4, B = 1, C = 1
requests = [
    ("A", "Today, explain DNS in one sentence."),
    ("A", "Today, explain VPN in one sentence."),
    ("A", "Today, explain HTTP in one sentence."),
    ("A", "Today, explain cloud computing in one sentence."),
    ("B", "Today, explain two-factor authentication in one sentence."),
    ("C", "Today, explain a firewall in one sentence."),
]


def send_request(user_id, prompt):
    payload = json.dumps({
        "user_id": user_id,
        "prompt": prompt
    }).encode("utf-8")

    request = Request(
        URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    start = time.perf_counter()

    try:
        with urlopen(request, timeout=300) as response:
            result = json.loads(response.read())

        elapsed = round(time.perf_counter() - start, 2)

        return {
            "user": user_id,
            "status": "SUCCESS",
            "source": result.get("source"),
            "request_id": result.get("request_id"),
            "elapsed": elapsed,
            "answer": result.get("response", "")[:100]
        }

    except HTTPError as error:
        return {
            "user": user_id,
            "status": f"HTTP {error.code}",
            "error": error.read().decode("utf-8")[:200]
        }

    except Exception as error:
        return {
            "user": user_id,
            "status": "FAILED",
            "error": str(error)
        }


print("Sending 6 concurrent requests to Smart AI Gateway...")
print("Employee A: 4 requests")
print("Employee B: 1 request")
print("Employee C: 1 request\n")

start_time = time.perf_counter()

with ThreadPoolExecutor(max_workers=6) as executor:

    futures = [
        executor.submit(send_request, user, prompt)
        for user, prompt in requests
    ]

    results = []

    # Print responses as they finish
    for future in as_completed(futures):
        result = future.result()
        results.append(result)
        print(result)


total_time = round(time.perf_counter() - start_time, 2)

successful = sum(
    1 for result in results
    if result["status"] == "SUCCESS"
)

print("\n========== TEST SUMMARY ==========")
print(f"Total requests: {len(requests)}")
print(f"Successful: {successful}")
print(f"Failed: {len(requests) - successful}")
print(f"Total elapsed time: {total_time}s")
print("==================================")

# Manual live-server demonstration. Importing this module never sends requests.

import asyncio
import json
from urllib.request import Request, urlopen


def send_request(user_id, prompt):
    payload = json.dumps({
        "user_id": user_id,
        "prompt": prompt
    }).encode("utf-8")

    request = Request(
        "http://127.0.0.1:8000/scheduler/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


async def main():
    requests = [
        ("A", "A1"),
        ("A", "A2"),
        ("A", "A3"),
        ("B", "B1"),
        ("B", "B2"),
        ("C", "C1")
    ]

    tasks = [
        asyncio.to_thread(send_request, user, prompt)
        for user, prompt in requests
    ]

    results = await asyncio.gather(*tasks)

    print("\nAll responses received:")

    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())

"""로컬 실행: python -m app

127.0.0.1에서만 수신한다 (R1.1). 공개 호스팅이나 외부 터널로 노출하지 말 것.
"""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=False)

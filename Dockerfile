# 番茄全站枚举器镜像
# 需要真浏览器（书库页是前端渲染 + 字体混淆），所以装 Chromium
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    FQ_DB=/data/fq_books.db \
    PIP_NO_CACHE_DIR=1

# 国内网络走清华源 / 阿里云 apt 镜像；构建时可用 --build-arg 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.aliyun.com

WORKDIR /app

# 系统依赖：Chromium 运行时 + 中文字体
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY *.py /app/
COPY static /app/static

VOLUME ["/data"]
EXPOSE 18081

# 默认：Web 控制台（部署后处于停止状态，需手动开始扫描）
CMD ["python", "-u", "fq_app.py"]

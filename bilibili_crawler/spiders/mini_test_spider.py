"""
最小化测试爬虫 - 验证 Scrapy engine 是否正确调用 async start()
"""
import sys
import logging
import scrapy

logger = logging.getLogger("test_spider")

class MiniTestSpider(scrapy.Spider):
    name = "mini_test"
    
    custom_settings = {
        "SCHEDULER": "scrapy.core.scheduler.Scheduler",
        "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
    }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        print("MINI: __init__ called", file=sys.stderr, flush=True)
        logger.info("MINI: __init__")
    
    # ★ Scrapy 2.16+ API: start_requests() 已弃用，改为 async start()
    async def start(self):
        print("MINI: async start() called!", file=sys.stderr, flush=True)
        logger.info("MINI: async start() called!")
        # Yield a simple test request
        yield scrapy.Request("http://httpbin.org/get", callback=self.parse)
    
    async def parse(self, response):
        print(f"MINI: parse called, status={response.status}", file=sys.stderr, flush=True)
        logger.info(f"MINI: parse called, status={response.status}")

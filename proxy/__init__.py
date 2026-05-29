"""
IP代理池模块

参考 MediaCrawler proxy/ 目录架构实现，提供:
- 代理供应商抽象基类 (BaseProxy)
- 代理IP池管理器 (ProxyIPPool)
- API客户端混入类 (ProxyRefreshMixin)
- 免费代理供应商

Usage:
    from proxy import ProxyIPPool, ProxyRefreshMixin
    
    pool = ProxyIPPool(pool_count=5)
    proxy = await pool.get_proxy()
"""

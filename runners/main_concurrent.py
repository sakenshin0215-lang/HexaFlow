import asyncio
import random
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexaflow.core.engine import HexaEngine

async def worker(engine: HexaEngine, worker_id: int, trace_file: str):
    """独立的并发工作线程（代表一个虚拟账号）"""
    
    # 🚀 细节 1：错峰启动防风控
    # 模拟真实场景下的错峰启动，避免所有账号在同一毫秒发起请求被拦截
    startup_delay = random.uniform(0, 3.0)
    print(f"🤖 [Worker {worker_id}] 准备就绪，将在 {startup_delay:.1f} 秒后启动...")
    await asyncio.sleep(startup_delay)
    
    # 🚀 细节 2：物理级隔离上下文
    # 为每个 Worker 创建一个绝对隔离的浏览器上下文 (独立的 Cookie、缓存)
    # 进阶玩法：未来这里可以为每个 worker 挂载不同的代理 IP (proxy) 
    context = await engine.browser.new_context(
        viewport={'width': 1280, 'height': 800},
        # 甚至可以给每个账号伪造不同的 User-Agent
        user_agent=f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 HexaWorker/{worker_id}"
    )
    
    print(f"🚀 [Worker {worker_id}] 开始执行黄金轨迹...")
    try:
        # 复用我们写好的极速确定性回放逻辑
        await engine.run_from_trace(trace_path=trace_file, context=context)
        print(f"✅ [Worker {worker_id}] 任务圆满完成！")
    except Exception as e:
        print(f"❌ [Worker {worker_id}] 任务崩溃: {e}")
    finally:
        # 为了演示效果，我们先不关闭 context，让你能同时看到多个浏览器的最终画面
        pass

async def main():
    # ⚠️ 请替换为你刚刚录制好的、极其稳定的那个 JSON 轨迹文件
    trace_file = "memory/workspace/traces/Task_20260302_183529_20260302_183620.json" 
    
    # 启动引擎 (headless=False 会同时弹出多个浏览器窗口，极其壮观)
    engine = HexaEngine(headless=False)
    await engine.start()
    
    # 设置并发数量 (Mac Studio 跑 3 个毫无压力，你可以自己往上加)
    CONCURRENCY = 3
    print(f"\n🔥 开启极限并发模式：瞬间唤醒 {CONCURRENCY} 个独立克隆体...\n")
    
    try:
        # 创建并发任务列表
        tasks = []
        for i in range(1, CONCURRENCY + 1):
            task = asyncio.create_task(worker(engine, worker_id=i, trace_file=trace_file))
            tasks.append(task)
            
        # 🚀 核心指令：并发执行所有任务，并等待它们全部跑完
        await asyncio.gather(*tasks)
        
        # 所有并发任务都完成后，才在主线程请求一次输入
        input("\n🎉 所有并发账号均已跑完轨迹！按回车键关闭所有浏览器并销毁引擎...")
        
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import os
from hexaflow.core.engine import HexaEngine

async def main():
    
    trace_file = "memory/workspace/traces/Task_20260303_151522_20260303_151748.json" 
    
    engine = HexaEngine(headless=False) 
    await engine.start()
    
    try:
        await engine.run_from_trace(trace_path=trace_file)
        
        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
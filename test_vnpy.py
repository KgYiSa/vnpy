"""
vnpy环境测试脚本
测试vnpy核心功能是否正常工作
"""

import sys
from datetime import datetime


def test_vnpy_import():
    """测试vnpy核心模块导入"""
    print("=" * 50)
    print("测试vnpy模块导入...")

    try:
        import vnpy
        print(f"✓ vnpy版本: {vnpy.__version__}")
    except ImportError as e:
        print(f"✗ vnpy导入失败: {e}")
        return False

    try:
        from vnpy.trader.engine import MainEngine
        print("✓ MainEngine导入成功")
    except ImportError as e:
        print(f"✗ MainEngine导入失败: {e}")
        return False

    try:
        from vnpy.event import EventEngine
        print("✓ EventEngine导入成功")
    except ImportError as e:
        print(f"✗ EventEngine导入失败: {e}")
        return False

    try:
        from vnpy.trader.object import TickData, BarData
        print("✓ 数据对象导入成功")
    except ImportError as e:
        print(f"✗ 数据对象导入失败: {e}")
        return False

    return True


def test_dependencies():
    """测试关键依赖库"""
    print("\n" + "=" * 50)
    print("测试关键依赖库...")

    dependencies = [
        ("numpy", "数值计算库"),
        ("pandas", "数据分析库"),
        ("talib", "技术分析库"),
        ("PySide6", "GUI界面库"),
        ("pyqtgraph", "图表库"),
        ("plotly", "可视化库")
    ]

    success_count = 0
    for lib_name, description in dependencies:
        try:
            if lib_name == "talib":
                import talib
            elif lib_name == "PySide6":
                import PySide6
            else:
                __import__(lib_name)
            print(f"✓ {lib_name}: {description}")
            success_count += 1
        except ImportError as e:
            print(f"✗ {lib_name}: {description} - 导入失败: {e}")

    print(f"\n依赖库测试结果: {success_count}/{len(dependencies)} 成功")
    return success_count == len(dependencies)


def test_basic_functionality():
    """测试基本功能"""
    print("\n" + "=" * 50)
    print("测试基本功能...")

    try:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        # 创建事件引擎
        event_engine = EventEngine()
        print("✓ 事件引擎创建成功")

        # 创建主引擎
        main_engine = MainEngine(event_engine)
        print("✓ 主引擎创建成功")

        # 测试数据对象创建
        from vnpy.trader.object import TickData
        from vnpy.trader.constant import Exchange

        tick = TickData(
            symbol="rb2501",
            exchange=Exchange.SHFE,
            datetime=datetime.now(),
            gateway_name="test"
        )
        print("✓ 数据对象创建成功")

        # 清理资源
        main_engine.close()
        event_engine.stop()
        print("✓ 资源清理成功")

        return True

    except Exception as e:
        print(f"✗ 基本功能测试失败: {e}")
        return False


def main():
    """主测试函数"""
    print("vnpy环境测试开始...")
    print(f"Python版本: {sys.version}")
    print(f"测试时间: {datetime.now()}")

    # 运行所有测试
    tests = [
        ("模块导入测试", test_vnpy_import),
        ("依赖库测试", test_dependencies),
        ("基本功能测试", test_basic_functionality)
    ]

    passed_tests = 0
    for test_name, test_func in tests:
        try:
            if test_func():
                passed_tests += 1
                print(f"\n{test_name}: ✓ 通过")
            else:
                print(f"\n{test_name}: ✗ 失败")
        except Exception as e:
            print(f"\n{test_name}: ✗ 异常 - {e}")

    print("\n" + "=" * 50)
    print(f"测试总结: {passed_tests}/{len(tests)} 项测试通过")

    if passed_tests == len(tests):
        print("🎉 恭喜！vnpy环境配置完全正确！")
        return True
    else:
        print("⚠️  部分测试失败，请检查安装配置")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

#!/usr/bin/env python3
"""
Test nieuwe threshold-based Eddi priority logica
"""
import os

# Set threshold mode
os.environ["EDDI_PRIORITY_MODE"] = "threshold"
os.environ["EDDI_RESERVE_W"] = "3000"
os.environ["BATTERY_MIN_EXPORT_W"] = "5000"

def test_threshold_logic():
    """Test verschillende export scenarios"""
    
    # Import the function after setting env vars
    import sys
    sys.path.insert(0, '.')
    from app import should_block_battery_for_eddi, extract_grid_export_w
    
    print("🧪 Testing Threshold-based Eddi Priority Logic")
    print("=" * 60)
    print("Configuratie:")
    print(f"   Eddi Reserve: 3000W (eerste 3kW voor Eddi)")
    print(f"   Battery Minimum: 5000W (batterij alleen bij >5kW)")
    print()
    
    # Test scenarios
    scenarios = [
        {"export": 1000, "expected": True, "desc": "Weinig zon - alles naar Eddi"},
        {"export": 2500, "expected": True, "desc": "Matige zon - nog steeds Eddi reserve"},
        {"export": 3500, "expected": True, "desc": "Net boven reserve - Eddi buffer zone"},
        {"export": 4500, "expected": True, "desc": "Bijna genoeg - nog in buffer zone"},
        {"export": 5500, "expected": False, "desc": "Veel zon - genoeg voor beide!"},
        {"export": 7000, "expected": False, "desc": "Veel zon - beide kunnen laden"},
        {"export": 0, "expected": True, "desc": "Geen export - batterij uit"},
        {"export": -500, "expected": True, "desc": "Import - batterij uit"},
    ]
    
    print("Test scenarios:")
    print("-" * 60)
    
    for scenario in scenarios:
        # Mock myenergi status
        mock_status = {
            "raw": [{
                "harvi": [{"ectp1": scenario["export"]}]
            }]
        }
        
        should_block, reason = should_block_battery_for_eddi(mock_status)
        
        # Check if result matches expectation
        status = "✅" if should_block == scenario["expected"] else "❌"
        action = "BLOCK" if should_block else "ALLOW"
        
        print(f"{status} Export: {scenario['export']:>5}W → {action:>5} | {scenario['desc']}")
        print(f"     Reason: {reason}")
        print()
    
    print("=" * 60)
    print("💡 Logica samenvatting:")
    print("   Export ≤ 3000W: Batterij UIT (Eddi reserve)")
    print("   Export 3000-5000W: Batterij UIT (Eddi buffer)")  
    print("   Export > 5000W: Batterij AAN (genoeg voor beide)")
    print()
    print("🎯 Voordelen:")
    print("   ✅ Eddi krijgt altijd eerste 3kW")
    print("   ✅ Buffer voorkomt conflict bij wisselend weer")
    print("   ✅ Batterij laadt alleen bij echt overschot")
    print("   ✅ Geen energie verspilling naar net")

if __name__ == "__main__":
    test_threshold_logic()

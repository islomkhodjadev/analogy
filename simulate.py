import pyautogui
import time
import random
import math

def natural_cursor_simulation(duration_hours=1):
    """
    Simulates more natural, human-like cursor movements
    """
    print(f"Starting natural cursor simulation for {duration_hours} hour(s)...")
    print("Press Ctrl+C to stop")
    
    end_time = time.time() + (duration_hours * 3600)
    screen_width, screen_height = pyautogui.size()
    
    try:
        while time.time() < end_time:
            # Get screen dimensions for realistic movement bounds
            screen_width, screen_height = pyautogui.size()
            
            # Simulate occasional long moves (like checking another monitor)
            if random.random() < 0.2:  # 20% chance
                # Move to a random position on screen
                target_x = random.randint(100, screen_width - 100)
                target_y = random.randint(100, screen_height - 100)
                
                # Move with human-like easing
                current_x, current_y = pyautogui.position()
                steps = random.randint(10, 20)
                
                for i in range(steps + 1):
                    t = i / steps
                    # Easing function for smoother movement
                    t = t * t * (3 - 2 * t)  # Smoothstep
                    
                    x = current_x + (target_x - current_x) * t
                    y = current_y + (target_y - current_y) * t
                    
                    pyautogui.moveTo(x, y, duration=0.01)
                    
            else:
                # Small jitter movements
                for _ in range(random.randint(1, 3)):
                    pyautogui.moveRel(
                        random.randint(-15, 15),
                        random.randint(-15, 15),
                        duration=0.1
                    )
                    time.sleep(0.2)
            
            # Wait between actions (simulates working)
            wait_time = random.randint(5, 25)  # 30-120 seconds
            print(f"Active, next movement in {wait_time//60}:{wait_time%60:02d} minutes")
            
            # Do nothing for the waiting period
            time.sleep(wait_time)
            
            # Optional: add key presses for more realistic activity
            if random.random() < 0.3:  # 30% chance
                pyautogui.press('shift')
                time.sleep(0.1)
                pyautogui.press('ctrl')
                
    except KeyboardInterrupt:
        print("\nSimulation stopped by user")

# Run for 8 hours
natural_cursor_simulation(8)
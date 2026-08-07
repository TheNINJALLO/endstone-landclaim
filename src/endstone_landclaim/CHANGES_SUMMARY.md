# Land Claim Plugin Improvements Summary

## Changes Made

### 1. Fixed Particle System
- **Replaced tip-based particle system** with proper Endstone `spawn_particle` method
- **Improved performance** by reducing particle density (every 8 blocks instead of 5)
- **Added fallback particles** if specific particle types fail
- **Used different particle types** based on protection status:
  - `minecraft:redstone_dust_particle` for protected claims
  - `minecraft:villager_happy` for unprotected claims
  - `minecraft:flame_particle` as fallback

### 2. Removed Protection Tips
- **Removed all tip notifications** when players try to break/place/interact in protected areas
- **Silent protection** - actions are cancelled without notification as requested

### 3. Added Admin Command System
- **New command**: `/lcadmin` for admin configuration
- **New permission**: `adminlandclaims.admin` for admin access
- **Configurable notification types**:
  - `toast` - Toast notifications (default)
  - `tip` - Tip messages
  - `chat` - Chat messages
- **Admin UI** accessible from main menu for operators
- **Command usage**: `/lcadmin notification <toast|tip|chat>`

### 4. Added Mob Killing Features
- **Three new claim flags**:
  - `kill_passive_mobs` - Kill passive mobs in claim
  - `kill_hostile_mobs` - Kill hostile mobs in claim  
  - `ignore_named_mobs` - Don't kill mobs with custom names
- **Automated mob killing** every 5 seconds using scheduled task
- **Command-based implementation** using `/kill` commands with selectors
- **Comprehensive mob lists**:
  - Hostile: zombie, skeleton, creeper, spider, enderman, etc.
  - Passive: cow, pig, sheep, chicken, villager, etc.

### 5. Updated Forms
- **Added mob killing toggles** to both create and edit claim forms
- **Updated form submission handlers** to process new toggle values
- **Maintained backward compatibility** with existing claims

### 6. Configuration System
- **New config file**: `config.json` in plugin data folder
- **Persistent notification type** setting
- **Automatic config loading/saving** on plugin enable/disable

### 7. Version Update
- **Updated version** to 9.1.0 to reflect new features
- **Updated version comment** to describe changes

## Technical Details

### New Data Structure
```python
@dataclass
class ClaimFlags:
    block_break: bool = True
    block_place: bool = True  
    interact: bool = True
    perimeter_particles: bool = True
    kill_passive_mobs: bool = False      # NEW
    kill_hostile_mobs: bool = False      # NEW
    ignore_named_mobs: bool = True       # NEW
```

### New Task System
- **Particle task**: Runs every 1 second (20 ticks)
- **Mob killer task**: Runs every 5 seconds (100 ticks)
- **Proper cleanup**: Both tasks cancelled on plugin disable

### Performance Optimizations
- **Reduced particle density** for better performance
- **Distance-based particle rendering** (64 block radius)
- **Error handling** for particle spawning failures
- **Efficient mob killing** using targeted commands

## Usage

### For Operators
1. Use `/lcadmin` to access admin settings
2. Change notification type via UI or command
3. Configure mob killing per claim in claim management

### For Players
- **Enter/leave notifications** now use configured type (toast/tip/chat)
- **No more protection tips** when actions are blocked
- **Visual particle boundaries** around claims (if enabled)

## Compatibility
- **Backward compatible** with existing claims
- **Automatic migration** of claim data structure
- **Graceful fallbacks** for missing configuration

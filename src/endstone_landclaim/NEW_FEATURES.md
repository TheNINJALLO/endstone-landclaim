# New Features in Land Claim Plugin v10.0.0

## 🎯 Claim Request System

### New Command: `/requestclaim`
**Permission**: `adminlandclaims.command` (default: all players)

Players can now request land claims instead of creating them directly. This allows operators to review and approve claims with custom protection settings.

#### How It Works
1. Player uses `/requestclaim` command
2. Player enters claim name and coordinates
3. System checks if claim is within 150 blocks of another player's claim and warns them
4. Request is submitted for operator review
5. Operator reviews request in admin menu and approves/denies
6. If approved, operator configures protection settings
7. Claim is created with player as owner

#### Features
- **2-Claim Limit**: Each player can have a maximum of 2 claims
- **Proximity Warnings**: Players are warned if their claim is within 150 blocks of another claim
- **Operator Control**: Operators can approve/deny requests and set protection levels
- **Automatic Notifications**: Players are notified when their request is approved or denied

### Admin Claim Request Management
- New button in admin menu: "Review Claim Requests"
- Shows count of pending requests
- Displays request details: player name, claim name, coordinates, area
- Operators can approve with custom protection settings or deny requests

## 🐺 Improved Mob Killing System

### Performance Optimization
- **Large Claim Safety**: Mob killing now only works when players are in the claim
- **Chunk-Based Killing**: Mobs are only killed within 2 chunks (32 blocks) of players
- **Prevents Server Crashes**: No longer attempts to kill mobs across entire large claims
- **Efficient Processing**: Only processes claims with players present

### Player-Controlled Toggles
Players can now enable/disable mob killing on their claims from the manage menu:
- **Kill Passive Mobs**: Toggle on/off
- **Kill Hostile Mobs**: Toggle on/off
- Changes take effect immediately

### How It Works
- Mob killing task runs every 3 seconds
- Only scans claims with players present
- Kills mobs within 32-block radius of each player
- Respects "Ignore Named Mobs" setting
- Uses efficient command-based approach

# New Features in Land Claim Plugin v9.1.0

## 🎆 Improved Particle System

### What Changed
- **Proper particle rendering** using Endstone's `spawn_particle` method
- **Performance optimized** with reduced particle density
- **Visual feedback** based on claim protection status

### Particle Types
- **Protected claims**: Red dust particles (`minecraft:redstone_dust_particle`)
- **Unprotected claims**: Happy villager particles (`minecraft:villager_happy`)
- **Fallback**: Flame particles if others fail

### Performance
- Particles spawn every 8 blocks (reduced from 5)
- Only visible to players within 64 blocks
- Automatic error handling and fallbacks

## 🔧 Admin Configuration System

### New Command: `/lcadmin`
**Permission**: `adminlandclaims.admin` (default: op)

#### Usage
```
/lcadmin                           # Open admin menu
/lcadmin notification <type>       # Set notification type
```

#### Notification Types
- **`toast`** - Toast notifications (default)
- **`tip`** - Tip messages  
- **`chat`** - Chat messages

### Admin Menu
- Accessible from main land claim menu (operators only)
- Easy switching between notification types
- Instant configuration saving

## 🐺 Mob Killing System

### New Claim Settings
Three new toggles available in claim creation and editing:

#### 1. Kill Passive Mobs
**Default**: Off
**Kills**: cow, pig, sheep, chicken, rabbit, horse, villager, etc.

#### 2. Kill Hostile Mobs  
**Default**: Off
**Kills**: zombie, skeleton, creeper, spider, enderman, witch, etc.

#### 3. Ignore Named Mobs
**Default**: On
**Effect**: Mobs with custom names are spared from killing

### How It Works
- **Automated**: Runs every 5 seconds
- **Command-based**: Uses `/kill` commands with precise selectors
- **Claim-specific**: Each claim has independent settings
- **Name-aware**: Respects custom mob names when enabled

### Mob Lists

#### Hostile Mobs
- zombie, skeleton, creeper, spider
- enderman, witch, blaze, ghast
- slime, magma_cube, wither_skeleton
- stray, husk, phantom, drowned
- pillager, vindicator, evoker, ravager
- vex, guardian, elder_guardian
- shulker, endermite, silverfish, cave_spider

#### Passive Mobs
- cow, pig, sheep, chicken, rabbit
- horse, donkey, mule, llama
- cat, wolf, parrot, ocelot, bat
- squid, dolphin, turtle, panda
- fox, bee, goat, axolotl, glow_squid
- villager, wandering_trader

## 🔇 Silent Protection

### What Changed
- **Removed all tip notifications** when protection blocks actions
- **Silent blocking** - actions are cancelled without messages
- **Cleaner experience** - no spam when testing claim boundaries

### Why This Matters
- Reduces notification clutter
- Allows players to discover boundaries naturally
- Particles provide visual feedback instead

## 📋 Form Updates

### Create Claim Form
- Added 3 new mob killing toggles
- Maintains all existing functionality
- Clear labeling and defaults

### Edit Claim Form  
- Added 3 new mob killing toggles
- Preserves existing claim settings
- Easy toggle management

## ⚙️ Configuration

### Config File: `config.json`
```json
{
  "notification_type": "toast"
}
```

### Automatic Management
- **Loading**: On plugin enable
- **Saving**: On plugin disable and setting changes
- **Defaults**: Safe fallbacks if config is missing

## 🔄 Backward Compatibility

### Existing Claims
- **Automatic migration** of data structure
- **Default values** for new mob killing flags
- **No data loss** during upgrade

### Existing Commands
- All existing commands work unchanged
- New admin command is additional functionality
- Permissions remain the same

## 🚀 Performance Improvements

### Particle System
- 37% fewer particles (8-block spacing vs 5-block)
- Distance-based rendering (64-block radius)
- Graceful error handling

### Mob Killing
- Efficient command-based approach
- 5-second intervals (not every tick)
- Targeted selectors for precision

### Task Management
- Proper task cleanup on disable
- Separate tasks for different functions
- Error isolation between systems

## 🎯 Usage Examples

### Setting Up a Mob-Free Base
1. Create claim around your base
2. Enable "Kill Hostile Mobs"
3. Enable "Kill Passive Mobs" if desired
4. Keep "Ignore Named Mobs" on to protect pets

### Configuring Notifications
1. Use `/lcadmin` as operator
2. Choose notification type:
   - **Toast**: Unobtrusive, modern
   - **Tip**: Always visible, classic
   - **Chat**: Permanent record

### Visual Claim Boundaries
1. Enable "Perimeter Particles" on claims
2. Particles show protection status:
   - **Red**: Protected claim
   - **Green**: Unprotected claim
3. Visible within 64 blocks

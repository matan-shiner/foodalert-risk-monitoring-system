"""GSFA (Codex Alimentarius General Standard for Food Additives) food
category taxonomy, used as the canonical `product_category` scheme for all
sources — replaces the earlier project-specific 13-bucket scheme.

Classification strategy: match against the DEEPEST (most specific) node
whose keywords appear in the alert's text, falling back to progressively
shallower parent nodes when no deep-enough signal exists. This is honest
about what garbled/translated recall text can actually support — most
records only justify a level-1 or level-2 classification (e.g. "08.0 Meat
and meat products" or "08.1 Fresh meat, poultry and game"); a smaller
subset states an explicit processing/preservation state (frozen, canned,
dried, smoked, fermented, cured, cooked) that lets it go deeper. Narrow
distinctions unlikely to ever be stated in recall text (e.g. "cheese rind
only" vs "whole ripened cheese", or "spirits >15% alcohol" vs "<15%") were
deliberately left without their own keyword rules — those records classify
at their nearest ancestor instead, which is the correct/expected fallback
behavior, not a bug.

Matched against each alert's title + product_description, whichever
language they're already in — all 13 sources either produce English
natively or have already been machine-translated to English by this point
in the pipeline, so a single English-only keyword set (unlike the earlier
per-language `_PRODUCT_CATEGORY_KW` dictionaries) is sufficient and keeps
one taxonomy build serving every source consistently.
"""
from __future__ import annotations
import re

# code -> (label, [keywords]). Keywords are omitted (empty list) for nodes
# that exist purely for taxonomy structure/labeling but aren't realistically
# distinguishable from recall text alone — those are still valid classification
# TARGETS (a deeper child can match), just never MATCHED directly themselves.
_NODES: dict[str, tuple[str, list[str]]] = {
    # 01.0 Dairy
    "01.0": ("Dairy products", ["dairy"]),
    "01.1": ("Milk and dairy-based drinks", ["milk", "dairy drink", "dairy-based drink"]),
    "01.1.1": ("Milk and buttermilk", []),
    "01.1.1.1": ("Milk, including sterilized and UHT", ["milk", "uht milk", "sterilized milk", "goats milk", "goat milk"]),
    "01.1.1.2": ("Buttermilk (plain)", ["buttermilk"]),
    "01.1.2": ("Dairy-based drinks, flavoured and/or fermented", ["chocolate milk", "cocoa milk", "eggnog", "drinking yoghurt", "drinking yogurt", "whey drink", "whey-based drink", "milk drink"]),
    "01.2": ("Fermented and renneted milk products (plain)", ["fermented milk", "renneted milk"]),
    "01.2.1": ("Fermented milks (plain)", ["fermented milk", "yogurt", "yoghurt", "kefir"]),
    "01.2.1.1": ("Fermented milks, not heat-treated after fermentation", []),
    "01.2.1.2": ("Fermented milks, heat-treated after fermentation", []),
    "01.2.2": ("Renneted milk", ["renneted milk", "rennet"]),
    "01.3": ("Condensed milk and analogues", ["condensed milk"]),
    "01.3.1": ("Condensed milk (plain)", []),
    "01.3.2": ("Beverage whiteners", ["beverage whitener", "coffee whitener", "creamer"]),
    "01.3.3": ("Sweetened condensed milk", ["sweetened condensed milk"]),
    "01.4": ("Cream (plain) and the like", ["cream"]),
    "01.4.1": ("Pasteurized cream", ["pasteurized cream"]),
    "01.4.2": ("Sterilized, UHT, whipping or whipped, reduced fat creams", ["whipping cream", "whipped cream", "uht cream", "sterilized cream", "reduced fat cream"]),
    "01.4.3": ("Clotted cream", ["clotted cream"]),
    "01.4.4": ("Cream analogues", ["cream analogue", "non-dairy cream", "coffee creamer"]),
    "01.5": ("Milk powder and cream powder", ["milk powder", "cream powder"]),
    "01.5.1": ("Milk powder and cream powder (plain)", []),
    "01.5.2": ("Milk and cream powder analogues", []),
    "01.5.3": ("Milk and cream (blend) powder", []),
    "01.6": ("Cheese", ["cheese"]),
    "01.6.1": ("Unripened cheese", ["unripened cheese", "fresh cheese", "cream cheese", "mozzarella", "cottage cheese", "ricotta"]),
    "01.6.2": ("Ripened cheese", ["ripened cheese", "cheddar", "gouda", "brie", "parmesan", "aged cheese"]),
    "01.6.2.1": ("Total ripened cheese, includes rind", []),
    "01.6.2.2": ("Rind of ripened cheese", []),
    "01.6.2.3": ("Cheese powder", ["cheese powder"]),
    "01.6.3": ("Whey cheese", ["whey cheese"]),
    "01.6.4": ("Processed cheese", ["processed cheese", "cheese spread", "cheese slice"]),
    "01.6.4.1": ("Plain processed cheese", []),
    "01.6.4.2": ("Flavoured processed cheese", []),
    "01.6.5": ("Cheese analogues", ["cheese analogue", "cheese substitute", "vegan cheese"]),
    "01.6.6": ("Whey protein cheese", ["whey protein cheese"]),
    "01.7": ("Dairy-based desserts", ["ice cream", "ice milk", "pudding", "flavoured yoghurt", "flavoured yogurt", "fruit yoghurt", "fruit yogurt", "dairy dessert", "gelato", "custard"]),
    "01.8": ("Whey and whey products", ["whey"]),

    # 02.0 Fats and oils
    "02.0": ("Fats and oils, and fat emulsions", ["fat", "oil"]),
    "02.1": ("Fats and oils essentially free from water", []),
    "02.1.1": ("Butter oil, anhydrous milkfat, ghee", ["ghee", "butter oil", "anhydrous milkfat"]),
    "02.1.2": ("Vegetable oils and fats", ["vegetable oil", "olive oil", "canola oil", "sunflower oil", "palm oil", "soybean oil", "corn oil", "sesame oil", "cooking oil"]),
    "02.1.3": ("Lard, tallow, fish oil, and other animal fats", ["lard", "tallow", "fish oil", "animal fat", "suet"]),
    "02.2": ("Fat emulsions mainly of type water-in-oil", []),
    "02.2.1": ("Emulsions containing at least 80% fat", []),
    "02.2.1.1": ("Butter and concentrated butter", ["butter"]),
    "02.2.1.2": ("Margarine and similar products", ["margarine", "butter-margarine blend"]),
    "02.2.2": ("Emulsions containing less than 80% fat", ["minarine", "low-fat spread"]),
    "02.3": ("Fat emulsions other than 02.2", ["fat emulsion", "spread"]),
    "02.4": ("Fat-based desserts", ["fat-based dessert"]),

    # 03.0 Edible ices
    "03.0": ("Edible ices, including sherbet and sorbet", ["ice cream", "sherbet", "sorbet", "edible ice", "popsicle", "ice lolly", "frozen dessert"]),

    # 04.0 Fruits and vegetables
    "04.0": ("Fruits and vegetables, mushrooms, roots/tubers, pulses/legumes, seaweeds, nuts and seeds", []),
    "04.1": ("Fruit", [
        "fruit", "apple", "banana", "orange", "mango", "grape", "melon",
        "watermelon", "strawberry", "pear", "peach", "lemon", "lime",
        "pineapple", "coconut", "durian", "lychee", "papaya", "kiwi",
        "plum", "cherry", "berries", "berry", "avocado", "fig", "date fruit",
    ]),
    "04.1.1": ("Fresh fruit", []),
    "04.1.1.1": ("Untreated fresh fruit", []),
    "04.1.1.2": ("Surface-treated fresh fruit", ["waxed fruit"]),
    "04.1.1.3": ("Peeled or cut fresh fruit", ["peeled fruit", "cut fruit", "sliced fruit", "fruit salad"]),
    "04.1.2": ("Processed fruit", []),
    "04.1.2.1": ("Frozen fruit", ["frozen fruit", "frozen berries"]),
    "04.1.2.2": ("Dried fruit", ["dried fruit", "raisin", "dried mango", "dried apricot", "sultana", "prune"]),
    "04.1.2.3": ("Fruit in vinegar, oil, or brine", ["pickled fruit", "fruit in brine"]),
    "04.1.2.4": ("Canned or bottled (pasteurized) fruit", ["canned fruit", "bottled fruit"]),
    "04.1.2.5": ("Jams, jellies, marmalades", ["jam", "jelly", "marmalade", "preserve"]),
    "04.1.2.6": ("Fruit-based spreads", ["chutney", "fruit spread"]),
    "04.1.2.7": ("Candied fruit", ["candied fruit", "glace fruit"]),
    "04.1.2.8": ("Fruit preparations", ["fruit pulp", "fruit puree", "fruit topping", "coconut milk"]),
    "04.1.2.9": ("Fruit-based desserts", ["fruit dessert"]),
    "04.1.2.10": ("Fermented fruit products", ["fermented fruit"]),
    "04.1.2.11": ("Fruit fillings for pastries", ["fruit filling"]),
    "04.1.2.12": ("Cooked or fried fruit", ["cooked fruit", "fried fruit"]),
    "04.2": ("Vegetables, mushrooms, roots/tubers, pulses/legumes, seaweeds, nuts and seeds", [
        "vegetable", "mushroom", "pulse", "legume", "tomato", "cucumber",
        "onion", "garlic", "potato", "carrot", "cabbage", "lettuce",
        "spinach", "eggplant", "pumpkin", "squash", "radish", "ginger",
        "celery", "bean sprout", "corn", "sweet potato", "broccoli",
        "cauliflower", "asparagus", "zucchini", "sprout",
    ]),
    "04.2.1": ("Fresh vegetables, and nuts and seeds", []),
    "04.2.1.1": ("Untreated fresh vegetables, and nuts and seeds", []),
    "04.2.1.2": ("Surface-treated fresh vegetables, and nuts and seeds", ["waxed vegetable"]),
    "04.2.1.3": ("Peeled, cut or shredded vegetables, and nuts and seeds", ["peeled vegetable", "cut vegetable", "shredded vegetable", "salad mix", "coleslaw"]),
    "04.2.2": ("Processed vegetables, seaweeds, and nuts and seeds", []),
    "04.2.2.1": ("Frozen vegetables", ["frozen vegetable", "frozen mixed vegetables"]),
    "04.2.2.2": ("Dried vegetables, seaweeds, and nuts and seeds", ["dried vegetable", "dried seaweed", "dried mushroom"]),
    "04.2.2.3": ("Vegetables and seaweeds in vinegar, oil, brine, or soy sauce", ["pickled vegetable", "vegetable in brine", "kimchi"]),
    "04.2.2.4": ("Canned or bottled or retort pouch vegetables", ["canned vegetable", "bottled vegetable"]),
    "04.2.2.5": ("Vegetable, nut and seed purees and spreads", ["peanut butter", "vegetable puree", "hummus", "nut butter"]),
    "04.2.2.6": ("Vegetable, nut and seed pulps and preparations", ["tofu", "bean curd", "soybean curd", "vegetable dessert"]),
    "04.2.2.7": ("Fermented vegetable products", ["fermented vegetable", "sauerkraut", "fermented tofu"]),
    "04.2.2.8": ("Cooked or fried vegetables and seaweeds", ["cooked vegetable", "fried vegetable"]),

    # 05.0 Confectionery
    "05.0": ("Confectionery", ["confectionery", "candy", "sweets"]),
    "05.1": ("Cocoa products and chocolate products", ["cocoa", "chocolate"]),
    "05.1.1": ("Cocoa mixes (powders and syrups)", ["cocoa powder", "cocoa mix", "cocoa syrup"]),
    "05.1.2": ("Cocoa-based spreads, incl. fillings", ["chocolate spread", "cocoa spread", "nutella"]),
    "05.1.3": ("Cocoa and chocolate products", ["chocolate bar", "milk chocolate", "white chocolate", "dark chocolate", "chocolate flakes"]),
    "05.1.4": ("Imitation chocolate, chocolate substitute products", ["imitation chocolate", "chocolate substitute"]),
    "05.2": ("Confectionery (hard/soft candy, nougats, etc.)", ["hard candy", "soft candy", "nougat", "toffee", "caramel", "lollipop", "gummy", "gummies", "marshmallow"]),
    "05.3": ("Chewing gum", ["chewing gum", "bubble gum"]),
    "05.4": ("Decorations, toppings (non-fruit) and sweet sauces", ["sprinkles", "cake decoration", "sweet sauce", "dessert topping"]),

    # 06.0 Cereals
    "06.0": ("Cereals and cereal products", ["cereal", "grain"]),
    "06.1": ("Whole, broken, or flaked grain, including rice", ["rice", "grain", "wheat grain", "barley", "oats", "quinoa"]),
    "06.2": ("Flours and starches", ["flour", "starch", "cornstarch", "cornflour"]),
    "06.3": ("Breakfast cereals, including rolled oats", ["breakfast cereal", "rolled oats", "oatmeal", "granola", "muesli"]),
    "06.4": ("Pastas and noodles and like products", ["pasta", "noodle", "rice paper", "vermicelli", "spaghetti", "macaroni"]),
    "06.4.1": ("Fresh pastas and noodles and like products", ["fresh pasta", "fresh noodle"]),
    "06.4.2": ("Pre-cooked or dried pastas and noodles and like products", ["dried pasta", "dried noodle", "instant noodle", "pre-cooked pasta"]),
    "06.5": ("Cereal and starch based desserts", ["rice pudding", "tapioca pudding", "starch dessert"]),
    "06.6": ("Batters", ["batter", "breading", "coating mix"]),
    "06.7": ("Rice cakes (Oriental type only)", ["rice cake", "mochi", "tteok"]),

    # 07.0 Bakery
    "07.0": ("Bakery wares", ["bakery"]),
    "07.1": ("Bread and ordinary bakery wares", []),
    "07.1.1": ("Breads and rolls", ["bread", "roll", "baguette", "loaf"]),
    "07.1.2": ("Crackers, excluding sweet crackers", ["cracker"]),
    "07.1.3": ("Other ordinary bakery products", ["bagel", "pitta", "pita", "english muffin", "flatbread", "naan"]),
    "07.1.4": ("Bread-type products, including bread stuffing and bread crumbs", ["bread stuffing", "bread crumb", "breadcrumb", "croutons"]),
    "07.1.5": ("Steamed breads and buns", ["steamed bun", "steamed bread", "baozi", "mantou"]),
    "07.2": ("Fine bakery wares", []),
    "07.2.1": ("Cakes, cookies and pies", ["cake", "cookie", "biscuit", "pie", "tart"]),
    "07.2.2": ("Other fine bakery products", ["doughnut", "donut", "sweet roll", "scone", "muffin", "croissant", "pastry", "danish"]),
    "07.2.3": ("Mixes for fine bakery wares", ["cake mix", "pancake mix", "baking mix"]),

    # 08.0 Meat
    "08.0": ("Meat and meat products, including poultry and game", ["meat", "poultry", "game meat"]),
    "08.1": ("Fresh meat, poultry and game", ["fresh meat", "fresh chicken", "fresh beef", "fresh pork", "raw meat", "raw chicken", "raw poultry"]),
    "08.1.1": ("Fresh meat, poultry and game, whole pieces or cuts", ["chicken breast", "beef steak", "pork chop", "chicken thigh", "chicken wing", "beef cut", "poultry cut"]),
    "08.1.2": ("Fresh meat, poultry and game, comminuted", ["ground beef", "ground pork", "ground chicken", "minced meat", "mince", "meat mince"]),
    "08.2": ("Processed meat, poultry, and game products in whole pieces or cuts", []),
    "08.2.1": ("Non-heat treated processed meat, poultry, and game products in whole pieces or cuts", []),
    "08.2.1.1": ("Cured (including salted) non-heat treated, whole pieces or cuts", ["cured meat", "salted meat", "prosciutto", "parma ham"]),
    "08.2.1.2": ("Cured and dried non-heat treated, whole pieces or cuts", ["dried meat", "beef jerky", "cured and dried", "biltong", "dried ham"]),
    "08.2.1.3": ("Fermented non-heat treated, whole pieces or cuts", ["fermented meat", "salami whole cut"]),
    "08.2.2": ("Heat-treated processed meat, poultry, and game products in whole pieces or cuts", ["ham", "roast beef", "roast chicken", "cooked ham", "smoked ham", "cooked poultry"]),
    "08.2.3": ("Frozen processed meat, poultry, and game products in whole pieces or cuts", ["frozen chicken", "frozen beef", "frozen pork", "frozen poultry"]),
    "08.3": ("Processed comminuted meat, poultry, and game products", []),
    "08.3.1": ("Non-heat treated processed comminuted meat, poultry, and game products", []),
    "08.3.1.1": ("Cured non-heat treated comminuted", ["cured sausage", "raw sausage"]),
    "08.3.1.2": ("Cured and dried non-heat treated comminuted", ["dried sausage", "pepperoni", "chorizo"]),
    "08.3.1.3": ("Fermented non-heat treated comminuted", ["fermented sausage", "salami"]),
    "08.3.2": ("Heat-treated processed comminuted meat, poultry, and game products", ["hot dog", "frankfurter", "bologna", "meatball", "burger patty", "sausage", "cooked sausage", "luncheon meat", "pate", "meat loaf"]),
    "08.3.3": ("Frozen processed comminuted meat, poultry, and game products", ["frozen meatball", "frozen burger", "frozen sausage"]),
    "08.4": ("Edible casings", ["sausage casing", "edible casing"]),

    # 09.0 Fish
    "09.0": ("Fish and fish products, including molluscs, crustaceans, and echinoderms", ["fish", "seafood", "mollusc", "mollusk", "crustacean", "shellfish"]),
    "09.1": ("Fresh fish and fish products, including molluscs, crustaceans, and echinoderms", ["fresh fish", "fresh seafood", "raw fish"]),
    "09.1.1": ("Fresh fish", ["fresh salmon", "fresh tuna", "fresh cod"]),
    "09.1.2": ("Fresh molluscs, crustaceans and echinoderms", ["fresh shrimp", "fresh prawn", "fresh crab", "fresh oyster", "fresh clam", "fresh mussel", "fresh scallop", "fresh lobster", "sea urchin"]),
    "09.2": ("Processed fish and fish products, including molluscs, crustaceans, and echinoderms", []),
    "09.2.1": ("Frozen fish, fish fillets, and fish products, including molluscs, crustaceans, and echinoderms", ["frozen fish", "frozen fillet", "frozen shrimp", "frozen prawn", "frozen crab", "frozen seafood", "frozen squid"]),
    "09.2.2": ("Frozen battered fish, fish fillets and fish products", ["battered fish", "fish sticks", "fish fingers", "breaded fish", "battered shrimp"]),
    "09.2.3": ("Frozen minced and creamed fish products", ["surimi", "fish paste frozen", "minced fish", "fish ball"]),
    "09.2.4": ("Cooked and/or fried fish and fish products, including molluscs, crustaceans, and echinoderms", []),
    "09.2.4.1": ("Cooked fish and fish products", ["cooked fish"]),
    "09.2.4.2": ("Cooked molluscs, crustaceans, and echinoderms", ["cooked shrimp", "cooked crab", "cooked shellfish", "boiled shrimp"]),
    "09.2.4.3": ("Fried fish and fish products, including molluscs, crustaceans, and echinoderms", ["fried fish", "fried shrimp", "fried calamari"]),
    "09.2.5": ("Smoked, dried, fermented, and/or salted fish and fish products, including molluscs, crustaceans, and echinoderms", ["smoked fish", "smoked salmon", "dried fish", "salted fish", "fermented fish", "anchovy", "dried squid", "fish jerky"]),
    "09.3": ("Semi-preserved fish and fish products, including molluscs, crustaceans, and echinoderms", []),
    "09.3.1": ("Marinated and/or in jelly", ["marinated fish", "fish in jelly", "pickled herring"]),
    "09.3.2": ("Pickled and/or in brine", ["pickled fish", "fish in brine"]),
    "09.3.3": ("Salmon substitutes, caviar, and other fish roe products", ["caviar", "fish roe", "salmon roe", "tobiko", "ikura"]),
    "09.3.4": ("Semi-preserved fish and fish products (e.g., fish paste), other", ["fish paste"]),
    "09.4": ("Fully preserved, including canned or fermented fish and fish products, including molluscs, crustaceans, and echinoderms", ["canned fish", "canned tuna", "canned salmon", "canned sardine", "tinned fish", "canned shrimp", "canned crab", "canned seafood"]),

    # 10.0 Eggs
    "10.0": ("Eggs and egg products", ["egg"]),
    "10.1": ("Fresh eggs", ["fresh egg", "raw egg"]),
    "10.2": ("Egg products", []),
    "10.2.1": ("Liquid egg products", ["liquid egg"]),
    "10.2.2": ("Frozen egg products", ["frozen egg"]),
    "10.2.3": ("Dried and/or heat coagulated egg products", ["dried egg", "egg powder"]),
    "10.3": ("Preserved eggs, including alkaline, salted, and canned eggs", ["preserved egg", "salted egg", "century egg", "pickled egg"]),
    "10.4": ("Egg-based desserts", ["custard", "egg tart", "egg pudding"]),

    # 11.0 Sweeteners
    "11.0": ("Sweeteners, including honey", ["sweetener", "sugar", "honey"]),
    "11.1": ("White and semi-white sugar, fructose, glucose, syrups", ["white sugar", "sucrose", "fructose", "glucose", "dextrose", "corn syrup", "molasses", "treacle"]),
    "11.2": ("Other sugars and syrups", ["brown sugar", "maple syrup", "cane sugar", "palm sugar"]),
    "11.3": ("Honey", ["honey"]),
    "11.4": ("Table-top sweeteners, including high-intensity sweeteners", ["stevia", "aspartame", "sucralose", "sweetener packet", "artificial sweetener"]),

    # 12.0 Salts, spices, soups, sauces
    "12.0": ("Salts, spices, soups, sauces, salads, protein products", []),
    "12.1": ("Salt", ["salt", "sea salt", "table salt"]),
    "12.2": ("Herbs, spices, seasonings and condiments", ["herb", "spice", "seasoning", "pepper", "cinnamon", "cumin", "paprika", "turmeric", "chili powder", "curry powder"]),
    "12.3": ("Vinegars", ["vinegar"]),
    "12.4": ("Mustards", ["mustard"]),
    "12.5": ("Soups and broths", ["soup", "broth", "stock"]),
    "12.5.1": ("Ready-to-eat soups and broths, including canned, bottled, and frozen", ["canned soup", "frozen soup", "instant soup"]),
    "12.5.2": ("Mixes for soups and broths", ["soup mix", "bouillon"]),
    "12.6": ("Sauces and like products", ["sauce", "gravy"]),
    "12.6.1": ("Emulsified sauces", ["mayonnaise", "salad dressing", "aioli"]),
    "12.6.2": ("Non-emulsified sauces", ["ketchup", "cheese sauce", "cream sauce", "brown gravy", "barbecue sauce", "tomato sauce", "pasta sauce", "salsa"]),
    "12.6.3": ("Mixes for sauces and gravies", ["sauce mix", "gravy mix"]),
    "12.6.4": ("Clear sauces", ["soy sauce", "fish sauce", "oyster sauce", "worcestershire"]),
    "12.7": ("Salads and sandwich spreads", ["macaroni salad", "potato salad", "coleslaw", "sandwich spread", "egg salad", "chicken salad", "tuna salad"]),
    "12.8": ("Yeast and like products", ["yeast", "baking powder", "baking soda"]),
    "12.9": ("Protein products", ["protein powder", "plant protein", "textured vegetable protein", "meat substitute", "protein isolate"]),

    # 13.0 Foodstuffs for particular nutritional uses
    "13.0": ("Foodstuffs intended for particular nutritional uses", []),
    "13.1": ("Infant formulae and follow-on formulae", ["infant formula", "follow-on formula", "baby formula"]),
    "13.2": ("Weaning foods for infants and growing children", ["weaning food", "baby food", "infant cereal", "toddler food"]),
    "13.3": ("Dietetic foods for special medical purposes", ["medical food", "dietetic food medical", "enteral formula"]),
    "13.4": ("Dietetic formulae for slimming purposes and weight reduction", ["slimming formula", "weight loss meal replacement", "diet shake"]),
    "13.5": ("Dietetic foods (other)", ["dietetic food", "special dietary food"]),
    "13.6": ("Food supplements", ["supplement", "vitamin", "capsule", "probiotic", "collagen", "protein supplement", "herbal supplement", "dietary supplement", "tablet supplement"]),

    # 14.0 Beverages
    "14.0": ("Beverages, excluding dairy products", ["beverage", "drink"]),
    "14.1": ("Non-alcoholic (\"soft\") beverages", []),
    "14.1.1": ("Waters", []),
    "14.1.1.1": ("Natural mineral waters and source waters", ["mineral water", "spring water", "natural water"]),
    "14.1.1.2": ("Table waters and soda waters", ["soda water", "sparkling water", "table water", "seltzer"]),
    "14.1.2": ("Fruit and vegetable juices", ["juice"]),
    "14.1.2.1": ("Canned or bottled (pasteurized) fruit juice", ["fruit juice", "orange juice", "apple juice", "grape juice"]),
    "14.1.2.2": ("Canned or bottled (pasteurized) vegetable juice", ["vegetable juice", "carrot juice", "tomato juice"]),
    "14.1.2.3": ("Concentrates (liquid or solid) for fruit juice", ["fruit juice concentrate"]),
    "14.1.2.4": ("Concentrates (liquid or solid) for vegetable juice", ["vegetable juice concentrate"]),
    "14.1.3": ("Fruit and vegetable nectars", ["nectar"]),
    "14.1.3.1": ("Canned or bottled (pasteurized) fruit nectar", ["fruit nectar"]),
    "14.1.3.2": ("Canned or bottled (pasteurized) vegetable nectar", ["vegetable nectar"]),
    "14.1.3.3": ("Concentrates (liquid or solid) for fruit nectar", []),
    "14.1.3.4": ("Concentrates (liquid or solid) for vegetable nectar", []),
    "14.1.4": ("Water-based flavoured drinks, including \"sport\"/\"electrolyte\" drinks", ["sports drink", "electrolyte drink", "flavoured water", "flavored water"]),
    "14.1.4.1": ("Carbonated drinks", ["soda", "cola", "carbonated drink", "soft drink", "pop"]),
    "14.1.4.2": ("Non-carbonated, including punches and ades", ["punch", "lemonade", "fruit ade", "iced tea drink"]),
    "14.1.4.3": ("Concentrates (liquid or solid) for drinks", ["drink concentrate", "cordial", "squash concentrate"]),
    "14.1.5": ("Coffee, coffee substitutes, tea, herbal infusions, and other hot cereal and grain beverages", ["coffee", "tea", "herbal infusion", "herbal tea", "chicory coffee"]),
    "14.2": ("Alcoholic beverages, including alcohol-free and low-alcoholic counterparts", ["alcohol", "alcoholic beverage"]),
    "14.2.1": ("Beer and malt beverages", ["beer", "malt beverage", "lager", "ale", "stout"]),
    "14.2.2": ("Cider and perry", ["cider", "perry"]),
    "14.2.3": ("Wines", ["wine"]),
    "14.2.3.1": ("Still wine", []),
    "14.2.3.2": ("Sparkling and semi-sparkling wines", ["sparkling wine", "champagne", "prosecco", "cava"]),
    "14.2.3.3": ("Fortified wine and liquor wine", ["fortified wine", "port wine", "sherry"]),
    "14.2.3.4": ("Aromatized wine", ["vermouth", "aromatized wine"]),
    "14.2.4": ("Fruit wine", ["fruit wine"]),
    "14.2.5": ("Mead", ["mead"]),
    "14.2.6": ("Spirituous beverages", ["spirits", "liquor", "whisky", "whiskey", "vodka", "rum", "gin", "brandy", "tequila", "sake", "soju", "baijiu", "shochu"]),
    "14.2.6.1": ("Spirituous beverages containing more than 15% alcohol", []),
    "14.2.6.2": ("Spirituous beverages containing less than 15% alcohol", []),

    # 15.0 Ready-to-eat savouries
    "15.0": ("Ready-to-eat savouries", ["snack"]),
    "15.1": ("Snacks - potato, cereal, flour or starch based", ["potato chip", "crisps", "corn chip", "tortilla chip", "pretzel", "puffed snack", "rice cracker snack"]),
    "15.2": ("Processed nuts, including coated nuts and nut mixtures", ["peanut", "almond", "cashew", "walnut", "pistachio", "roasted nut", "coated nut", "trail mix", "nut mixture"]),
    "15.3": ("Snacks - fish based", ["fish snack", "dried squid snack", "fish jerky snack"]),

    # 16.0 Composite foods
    "16.0": ("Composite foods", ["casserole", "meat pie", "mincemeat", "ready meal", "frozen meal", "sandwich", "burger", "pizza", "curry", "stew", "bento", "instant noodle meal", "prepared meal", "microwave meal", "lasagna", "quiche"]),
}

def _depth(code: str) -> int:
    """Codex codes use a trailing ".0" as the top-level placeholder (e.g.
    "01.0" IS level 1, not level 2), so a naive dot-count can't tell "02.0"
    (level 1) apart from "09.4" (level 2) — both have exactly one dot. Strip
    trailing ".0" segments before counting."""
    parts = code.split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts = parts[:-1]
    return len(parts)


# Ordered deepest-first so the classifier tries the most specific match
# before falling back to a shallower ancestor.
_ORDERED_CODES = sorted(_NODES.keys(), key=lambda c: (-_depth(c), c))

_COMPILED_RULES: list[tuple[str, "re.Pattern"]] = [
    (code, re.compile(r"\b(?:" + "|".join(re.escape(kw) + r"(?:es|s)?" for kw in keywords) + r")\b", re.IGNORECASE))
    for code, (label, keywords) in _NODES.items()
    if keywords
]
_COMPILED_RULES.sort(key=lambda item: (-_depth(item[0]), item[0]))


def label_for(code: str) -> str:
    label, _ = _NODES.get(code, (code, []))
    return label


def format_category(code: str) -> str:
    """"08.1 Fresh meat, poultry and game" — the string actually stored in
    product_category, keeping both the code (sortable/groupable) and a
    human-readable label in one field without a schema change."""
    return f"{code} {label_for(code)}"


def classify(text: str) -> str | None:
    """Deepest-first keyword match against `text`. Returns a formatted
    "<code> <label>" string, or None if nothing in the taxonomy matches at
    all (as opposed to matching only at a shallow level, which is a normal,
    expected outcome — not a failure)."""
    if not text:
        return None
    for code, pattern in _COMPILED_RULES:
        if pattern.search(text):
            return format_category(code)
    return None


def all_codes() -> list[str]:
    return list(_NODES.keys())


def top_level_category(stored_value: str | None) -> str | None:
    """Roll a full-depth stored value (e.g. "08.1.1 Fresh meat, poultry and
    game, whole pieces or cuts") up to its top-level "<code>.0 <label>" form
    (e.g. "08.0 Meat and meat products, including poultry and game").

    Full-depth classification is kept in the DB for future use (finer
    filtering/analysis later); the dashboard only ever displays this
    rolled-up top-level form — see generate_dashboard.py, which registers
    this as a SQLite function so every query groups/labels by the top-level
    category directly, rather than rolling up already-grouped fine-grained
    counts (which would double-count/fragment groups that should merge).
    """
    if not stored_value:
        return None
    code = stored_value.split(" ", 1)[0].strip()
    top_code = code.split(".")[0] + ".0"
    if top_code not in _NODES:
        return stored_value  # not a recognized GSFA code — pass through as-is
    return format_category(top_code)

from django.contrib import admin
from .models import *

# Register your models here.

class ItemAdmin(admin.ModelAdmin):
	list_display=['title','price','discount_price']
	prepopulated_fields={'slug':('title',)}

class SettlementAdmin(admin.ModelAdmin):
	list_display=['name','region','distric','ward','phone']


admin.site.register(Contact)
admin.site.register(Category)
admin.site.register(Item,ItemAdmin)
admin.site.register(OrderItem)
admin.site.register(Order)
admin.site.register(Leaders)
admin.site.register(Settlement,SettlementAdmin)

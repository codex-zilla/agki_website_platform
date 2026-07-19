from django.db import models
from useraccount.models import CustomUser
from django.urls import reverse

# Create your models here.
class Contact(models.Model):
	name = models.CharField(max_length=20)
	email = models.CharField(max_length=30)
	subject = models.CharField(max_length=50)
	message = models.TextField(max_length=500)

	def __str__(self):
		return self.name

class Leaders(models.Model):
	name = models.CharField(max_length=20)
	image = models.ImageField()
	position = models.CharField(max_length=50)
	facebook = models.CharField(max_length=50)
	twitter = models.CharField(max_length=30)
	instagram = models.CharField(max_length=50)
	whatsapp = models.CharField(max_length=50)

	def __str__(self):
		return self.name

class Settlement(models.Model):
    name = models.CharField(max_length=20)
    region = models.CharField(max_length=20)
    distric = models.CharField(max_length=20)
    ward = models.CharField(max_length=20)
    street = models.CharField(max_length=20)
    phone = models.CharField(max_length=20)




class Category(models.Model):
    name = models.CharField(max_length=200,db_index=True)
    slug=models.SlugField(unique=True)
    description = models.TextField()
    is_latest=models.BooleanField(default=False)

    def __str__(self):
        return self.name

    #HII NI KWA AJILI YA KUPATA PRODUCT ZILIZOPO KWENYE CATEGORY HUSIKA
    def get_absolute_url(self):
        return reverse('product_by_category', args=[self.slug])



class Item(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    title = models.CharField(max_length=200,db_index=True)
    slug = models.SlugField()
    price = models.IntegerField()
    discount_price = models.IntegerField(blank=True, null=True)
    status = models.CharField(max_length=200)
    description = models.TextField()
    image = models.ImageField(upload_to='productImages', blank=True, null=True)
    available=models.BooleanField(default=True)
    created=models.DateTimeField(auto_now_add=True)
    updated=models.DateTimeField(auto_now_add=True)
    is_latest=models.BooleanField(default=False)
    is_featured=models.BooleanField(default=False)
    

    def __str__(self):
        return self.title
    #HII NI KWA AJILI YA KUADI ITEM TO A CART
    def get_add_to_cart_url(self):
        return reverse('add_to_cart', kwargs={'id': self.id})

    #HII NI KWA AJILI YA KUREMOVE ITEM FROMA CART
    def get_remove_from_cart_url(self):
        return reverse('remove_from_cart', kwargs={'id': self.id})
    #HII NI KWA AJILI YA KUREMOVE ITEM MOJAMOJA FROM A CART ENDAPO UTACLICK MINUS SIGN
    def get_remove_single_from_cart_url(self):
        return reverse('remove_single_from_cart', kwargs={'id': self.id})




class OrderItem(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    ordered = models.BooleanField(default=False)
    quantity = models.IntegerField(default=1)

    def __str__(self):
        return f"{self.quantity} of {self.item.title}"
    def get_total_item_price(self):
        return self.quantity * self.item.price

    def get_total_item_discount_price(self):
        return self.quantity * self.item.discount_price

    def get_amount_saved(self):
        return self.get_total_item_price() - self.get_final_price()

    def get_final_price(self):
        if self.item.discount_price:
            return self.get_total_item_discount_price()
        return self.get_total_item_price()



class Order(models.Model):
    #category = models.ForeignKey(Category, on_delete=models.CASCADE)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    items = models.ManyToManyField(OrderItem)
   
    ordered = models.BooleanField(default=False)
    start_date = models.DateTimeField(auto_now_add=True)
    ordered_date = models.DateTimeField()
    def __str__(self):
    	return self.user.username
    def get_total(self):
        total = 0
        for order_item in self.items.all():
            total += order_item.get_final_price()

        return total





    
    

    




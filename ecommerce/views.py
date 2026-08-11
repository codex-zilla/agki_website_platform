from django.shortcuts import render,redirect,get_object_or_404
from django.contrib import messages
from ecommerce.forms import *
from django.views.generic import CreateView, DetailView, DeleteView, UpdateView, ListView, View
#from django.views import generic
#from django.views.generic.base import TemplateView
from django.urls import reverse_lazy
from django.utils import timezone
#from django.contrib.auth.decorators import login_required
#from django.contrib.auth.mixins import LoginRequiredMixin
#from django.core.exceptions import ObjectDoesNotExist
from .models import *
from django.contrib.auth import authenticate, login, logout

# Create your views here.
def home(request,category_slug=None):
	category = None
	categories = Category.objects.all()
	item = Item.objects.all().order_by('-id')
	if category_slug:
		category = get_object_or_404(Category,slug=category_slug)
		item = item.filter(category=category)
	context = {
		'item':item,
		'categories':categories,
		'category':category
	}

	return render(request, 'ecommerce/home.html',context)

def ourproduct(request,category_slug=None):
	category = None
	categories = Category.objects.all().order_by('-id')
	item = Item.objects.all().order_by('-id')
	if category_slug:
		category = get_object_or_404(Category,slug=category_slug)
		item = item.filter(category=category)
	context = {
		'item':item,
		'categories':categories,
		'category':category
	}

	return render(request, 'ecommerce/ourproduct.html',context)

def base(request):
	return render(request, 'ecommerce/base.html')

def blog(request):
	return render(request, 'ecommerce/blog.html')



def contact(request):
	form = ContactForm()
	if request.method=='POST':
		form = ContactForm(request.POST)
		if form.is_valid():
			form.save()
			messages.success(request,'Message sent successfully,thanks for contacting to us')
			return redirect('ecommerce_home')
	context = {
		'form':form
	}
	return render(request, 'ecommerce/contact.html',context)

def settlement(request):
	form = SettlementForm()
	if request.method=='POST':
		form = SettlementForm(request.POST)
		if form.is_valid():
			form.save()
			messages.success(request,'your detail updated successfully, we will contact with you')
			return redirect('order_summary')
	context = {
		'form':form
	}
	return render(request,'ecommerce/settlement.html',context)



def about(request):
	x = Leaders.objects.all()
	context = {
		'x':x
	}
	return render(request, 'ecommerce/about.html',context)

def detailed_product(request,id):
	item = Item.objects.all().order_by('-id')
	detailed_product = Item.objects.get(id=id)
	context = {
		'detailed_product':detailed_product,
		'item':item
	}
	return render(request, 'ecommerce/detailed_product.html',context)

def receivedmessage(request):
	x = Contact.objects.all()
	context = {
		'x':x
	}
	return render(request,'ecommerce/receivedmessage.html',context)

def add_to_cart(request, id):
    item = get_object_or_404(Item, id=id)
    quantity = int(request.POST.get('quantity', 1))

    # Get existing order item safely
    order_item_qs = OrderItem.objects.filter(
        item=item,
        user=request.user,
        ordered=False
    )

    if order_item_qs.exists():
        order_item = order_item_qs.first()
        order_item.quantity += quantity
        order_item.save()
    else:
        order_item = OrderItem.objects.create(
            item=item,
            user=request.user,
            ordered=False,
            quantity=quantity
        )

    order, created = Order.objects.get_or_create(
        user=request.user,
        ordered=False,
        defaults={'ordered_date': timezone.now()}
    )

    order.items.add(order_item)
    messages.success(request, f"{item} was added to your cart")
    return redirect('order_summary')



def remove_from_cart(request, id):
    item = get_object_or_404(Item, id=id)
    order_item, created = OrderItem.objects.get_or_create(
        item=item, user=request.user, ordered=False)
    order_qs = Order.objects.filter(user=request.user, ordered=False)
    if order_qs.exists():
        order = order_qs[0]
        if order.items.filter(item_id=item.id).exists():
            order.items.remove(order_item)
            order.save()
            messages.success(request, f"{item.title} was removed from your Order")
                
            return redirect('order_summary')
        else:
            messages.info(request, f"{item.title} was not in your Order")
            return redirect('order_summary')
    else:
        messages.info(request, "You don't have an active order!")
        return redirect('order_summary')

class OrderSummaryView(View):
    def get(self, *args, **kwargs):
        order = Order.objects.get(user=self.request.user, ordered=False)
        context = {
            'order':order
        }
        return render(self.request, 'ecommerce/order_summary.html',context)
    
def remove_single_from_cart(request, id):
    item = get_object_or_404(Item, id=id)
    order_item, created = OrderItem.objects.get_or_create(
        item=item, user=request.user, ordered=False)
    order_qs = Order.objects.filter(user=request.user, ordered=False)
    if order_qs.exists():
        order = order_qs[0]
        if order.items.filter(item__id=item.id).exists():
            if order_item.quantity > 1:
                order_item.quantity -= 1
                order_item.save()
            else:
                order.items.remove(order_item)
                order.save()
            messages.success(request, f"{item}'s quantity was updated")
            return redirect('order_summary')
        else:
            messages.info(request, f"{item.title} was not in your Order")
            return redirect('order_summary')
    else:
        messages.info(request, "You don't have an active Order!")
        return redirect('order_summary')
	

def item_add(request):
	category = Category.objects.all()
	items = Item.objects.all()
	form = ItemForm()
	if request.method == 'POST':
		form = ItemForm(request.POST, request.FILES)
		if form.is_valid():
			form.save()
			messages.success(request,'Item Added successfully')
			return redirect('ecommerce_items')
	context = {
		'form':form,
		'category':category,
		'items':items,
	}
	return render(request,'ecommerce/item_add.html',context)


def signin3(request):
    if request.method == 'POST':
        username = request.POST['username']
        password1 = request.POST['password1']

        # Check if username exists
        if not CustomUser.objects.filter(email=username).exists():
            messages.error(request, 'No account found with this username.')
            return redirect('signin3')

        user = authenticate(username=username, password=password1)

        if user is not None:
            if user.is_active:
                login(request, user)
                return redirect('ourproduct')
            else:
                messages.warning(request, 'Your account is not verified yet. Please check your email for OTP verification.')
                return redirect('signin3')
            
        else:
            messages.error(request, 'Incorrect password. Please try again.')
            return redirect('signin3')
        
    return render(request, 'ecommerce/signin.html')

def logout3(request):
	logout(request)
	return redirect('ourproduct')

def signup2(request):
    if request.method == 'POST':
        username = request.POST['username']
        firstname = request.POST['firstname']
        lastname = request.POST['lastname']
        email = request.POST['email']
        # added this
        phone = request.POST['phone']
        # end added
        password1 = request.POST['password1']
        password2 = request.POST['password2']

        # Check if email already exists
        if CustomUser.objects.filter(email=email).exists():
            messages.error(request, 'An account with this email already exists. Please use another email or log in.')
            return redirect('signup2')

        # Check if username already exists (optional but recommended)
        if CustomUser.objects.filter(username=username).exists():
            messages.error(request, 'This username is already taken. Please choose another one.')
            return redirect('signup2')

        # Check if passwords match
        if password1 != password2:
            messages.error(request, 'Passwords do not match. Please try again.')
            return redirect('signup2')

        # Create user but don't activate yet
        myuser = CustomUser.objects.create_user(username=username, email=email, password=password1)
        myuser.first_name = firstname
        myuser.last_name = lastname
        myuser.phone = phone
        # myuser.is_active = False  # Explicitly set to False
        myuser.save()  # This triggers the post_save signal
        
        messages.success(request, 'Account created successfully!')
        return redirect('signin3')

    return render(request, 'ecommerce/signup.html')
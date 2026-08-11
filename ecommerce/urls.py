
from django.urls import path
from . import views

urlpatterns = [
    # Named 'ecommerce_home', not 'home': this app is included after app.urls
    # in project/urls.py, and both used to register the name 'home'. Django
    # resolves a duplicate name to the LAST one registered, so {% url 'home' %}
    # site-wide silently pointed at the shop instead of the marketing
    # homepage - breaking the "Home" links in useraccount's signup and
    # pre-approval pages and the admin's "View live site" link. Same class of
    # collision the design doc flagged for 'signin' (§1). The URL path is
    # unchanged; only the reversible name differs.
    path('home', views.home, name='ecommerce_home'),
    path('<slug:category_slug>', views.ourproduct, name='product_by_category'),
    path('base/', views.base, name='base'),
    path('blog/', views.blog, name='blog'),
    path('', views.ourproduct, name='ourproduct'),
    path('settlement/', views.settlement, name='settlement'),
    path('contact/', views.contact, name='contact'),
    path('about/', views.about, name='about'),
    path('receivedmessage/', views.receivedmessage, name='receivedmessage'),
    path('detailed_product/<int:id>/', views.detailed_product, name='detailed_product'),
    path('item_add/', views.item_add, name='item_add'),
    path('add_to_cart/<int:id>/', views.add_to_cart, name="add_to_cart"),
    path('remove_from_cart/<int:id>/', views.remove_from_cart, name="remove_from_cart"),
    path('remove_single_from_cart/<int:id>/', views.remove_single_from_cart, name="remove_single_from_cart"),
    path('order_summary/', views.OrderSummaryView.as_view(), name='order_summary'),
    # path('order_success/<int:id>/', views.order_success, name='order_success'),
    path('signin/', views.signin3, name='signin3'),
    path('signup/', views.signup2, name='signup2'),
    path('logout/', views.logout3, name='logout3'),

]

